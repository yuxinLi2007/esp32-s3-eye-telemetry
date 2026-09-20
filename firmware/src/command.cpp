#include "command.h"

#include <WiFi.h>

#include "config.h"
#include "sensors.h"
#include "uplink.h"

// ------------------------------------------------------------------ 常量
// 自检的判据不是"读得到就算好"，而是"读到的值在物理上说得通"：
// 板子静止时加速度合矢量必然接近 1 g。只检查 I2C 有没有应答的话，
// 一个量程配错、输出恒为 0 的通道会被判成"通过"，那比不检查更坏。
#define SELFTEST_ACCEL_READS  20
#define SELFTEST_MIC_READS    10
#define SELFTEST_MAG_TOL      0.15f   // |a| 偏离 1 g 的容许范围（见 config.h 标定说明）
#define SELFTEST_MIN_OK_RATIO 0.8f    // 允许的偶发读失败比例

// 比服务端 timeout_ms 早这么多收手，留出"上传 + 回执"的时间。
// 不然会出现：板子以为自己还在正常执行，服务端已经判了 timeout，
// 姗姗来迟的 done 被 409 拒收——数据其实采到了，界面上却写着超时。
#define CAPTURE_MARGIN_MS     3000UL

#define CLAIM_HTTP_TIMEOUT_MS 3000UL
#define RESULT_HTTP_TIMEOUT_MS 5000UL

// 指令样本自己的 seq 从 0 起算，与连续流互不相干。
// 服务端 find_gaps 按 boot_id#request_id 分流，两条流各自检查连续性。
#define MAX_CMD_SAMPLES       200     // 与服务端 OPS["capture"]["params"]["n"]["hi"] 一致

static SnapshotFn g_snap = nullptr;
static IdleFn g_idle = nullptr;
static uint32_t g_last_poll_ms = 0;
static bool g_executing = false;
static bool g_token_rejected = false;   // 只为把 401 说一次，不每 2 秒刷一行

// 已经执行过的 request_id。领取接口对同一次启动是幂等的：设备再来领，服务端
// 会把同一条原样还回来（连 running 状态的也还）。所以"领到了"不等于"有新活干"。
static char g_done_rid[40];

// 发失败的终态回执（done/failed）缓存。早先没有这套东西：回执一旦失败，
// 下一个轮询周期幂等重领到同一条，固件就当成新任务把采集从头再跑一遍，
// 跑完又发一轮 progress，把服务端的静默计时不停刷新——于是那条指令永远停在
// running，永远不会 timeout，后面所有新指令全被堵在队里。真机联调时这一条
// 卡了 16 分钟、攒了 600 多条 running->running 事件。终态必须靠"重发回执"兜底，
// 绝不能靠"重跑一遍"兜底。
static char g_pend_rid[40];
static String g_pend_body;
static uint8_t g_pend_tries = 0;
static const uint8_t PEND_RETRY_MAX = 5;   // 放弃后由服务端判 timeout，界面上看得见

struct Command {
  char request_id[24];
  char op[16];
  long n;
  long interval_ms;
  uint32_t timeout_ms;
};

static DeviceSnapshot snap() {
  DeviceSnapshot s = {};
  if (g_snap) g_snap(&s);
  return s;
}

// 采集期间让主循环继续跑：连续流照常采样、照常上传，
// 于是"远程采集"不会在连续流上留下一个假的断层。
static void idle() {
  if (g_idle) g_idle();
}

static void idle_for(uint32_t ms) {
  uint32_t t0 = millis();
  while (millis() - t0 < ms) {
    idle();
    delay(2);
  }
}

// ------------------------------------------------------------------ 回执
// 回执体是 JSON（服务端要的是结构化结果），但板上仍然不引 JSON 库：
// 手写拼接 + json_escape 就够，而且字段是固定的几个。
static String result_open(const char *state, const DeviceSnapshot &s) {
  String j;
  j.reserve(512);
  j += "{\"state\":\"";
  j += state;
  j += "\",\"boot_id\":\"";
  j += s.boot_id ? s.boot_id : "";
  j += "\",\"device_mac\":\"";
  j += s.mac ? s.mac : "";
  j += "\"";
  return j;
}

// 返回 true 表示服务端收下了这次状态变更。
static bool post_result_to(const char *request_id, const String &json) {
  String path = String("/api/v1/commands/") + request_id + "/result";
  String resp;
  int code = uplink_post(path.c_str(), json, "application/json", &resp,
                         RESULT_HTTP_TIMEOUT_MS);
  if (code != 200) {
    Serial.printf("[cmd] 回执失败 req=%s HTTP %d resp=%s\n", request_id, code,
                  resp.c_str());
    return false;
  }
  return true;
}

// 发终态回执（done/failed）。失败就缓存下来，由 command_poll 在后续周期重发。
static void post_terminal(const char *request_id, const String &json) {
  if (post_result_to(request_id, json)) {
    g_pend_body = String();
    g_pend_rid[0] = '\0';
    g_pend_tries = 0;
    return;
  }
  // 缓存体积有限，宁可丢掉旧的也不要撑爆堆：终态回执本体不大（几百字节）。
  g_pend_body = json;
  strncpy(g_pend_rid, request_id, sizeof(g_pend_rid) - 1);
  g_pend_rid[sizeof(g_pend_rid) - 1] = '\0';
  g_pend_tries = 0;
}

// 进度心跳。它同时刷新服务端的超时锚点（deadline 以"最后一次有动静"为准），
// 所以一次合法的长采集不会被误判超时。失败只记日志，不打断采集：
// 心跳发不出去通常意味着网络已经断了，那时候最该做的是把数据采完并尝试上传。
static void post_progress(const char *request_id, int pct) {
  DeviceSnapshot s = snap();
  String j = result_open("running", s);
  j += ",\"progress\":";
  j += String(pct);
  j += "}";
  if (!post_result_to(request_id, j)) {
    Serial.printf("[cmd] 进度 %d%% 未能上报（继续执行）\n", pct);
  }
}

static void post_failed(const char *request_id, const char *error_code,
                        const char *error_message, const String &result_json) {
  DeviceSnapshot s = snap();
  String j = result_open("failed", s);
  j += ",\"error_code\":\"";
  j += json_escape(error_code);
  j += "\"";
  if (error_message) {
    j += ",\"error_message\":\"";
    j += json_escape(error_message);
    j += "\"";
  }
  if (result_json.length()) {
    j += ",\"result\":";
    j += result_json;
  }
  j += "}";
  post_terminal(request_id, j);
  Serial.printf("[cmd] 已回报失败 req=%s code=%s\n", request_id, error_code);
}
// ------------------------------------------------------------------ 领取
static bool split_claim(const String &body, String parts[4]) {
  int start = 0, idx = 0;
  while (idx < 3) {
    int k = body.indexOf('|', start);
    if (k < 0) return false;
    parts[idx++] = body.substring(start, k);
    start = k + 1;
  }
  if (body.indexOf('|', start) >= 0) return false;  // 多于 4 段 = 格式变了，别猜
  parts[3] = body.substring(start);
  // 参数段 parts[2] 允许为空：ping/selftest 本来就没有参数，服务端发的是
  // "rid|op||timeout_ms"。早先这里对四段一律要求非空，导致这两条指令永远
  // 解析失败、永远不回执，在服务端只能等成 timeout——真机联调才暴露出来，
  // 因为仿真器的解析比固件宽松。其余三段为空一律当作应答被截断，宁可不执行。
  if (!parts[0].length() || !parts[1].length() || !parts[3].length()) return false;
  return true;
}

static bool parse_claim(const String &body, Command &out) {
  String parts[4];
  if (!split_claim(body, parts)) return false;
  if (!parts[0].startsWith("req_")) return false;
  if (parts[0].length() >= sizeof(out.request_id)) return false;
  if (parts[1].length() >= sizeof(out.op)) return false;

  memset(&out, 0, sizeof(out));
  strcpy(out.request_id, parts[0].c_str());
  strcpy(out.op, parts[1].c_str());
  out.n = 40;             // 与服务端 OPS["capture"] 的 default 一致
  out.interval_ms = 50;
  out.timeout_ms = (uint32_t)parts[3].toInt();
  if (out.timeout_ms < 1000) return false;   // 不合理的超时 = 应答被截断，宁可不执行

  // 参数段可以为空（ping/selftest 没有参数）
  String kv = parts[2];
  int start = 0;
  while (start < (int)kv.length()) {
    int semi = kv.indexOf(';', start);
    String item = semi < 0 ? kv.substring(start) : kv.substring(start, semi);
    int eq = item.indexOf('=');
    if (eq > 0) {
      String k = item.substring(0, eq), v = item.substring(eq + 1);
      if (k == "n") out.n = v.toInt();
      else if (k == "interval_ms") out.interval_ms = v.toInt();
      // 不认识的参数直接忽略：服务端加参数不该让老固件崩掉，
      // 但也不能假装执行了——所以最终回执里会带上固件版本，人对得上号。
    }
    if (semi < 0) break;
    start = semi + 1;
  }
  if (out.n < 1 || out.n > MAX_CMD_SAMPLES) return false;
  if (out.interval_ms < 1 || out.interval_ms > 5000) return false;
  return true;
}

// 领取一条指令。返回 false 表示"这次没有活干"（含网络失败，两者都不该刷屏）。
static bool try_claim(Command &out) {
  DeviceSnapshot s = snap();
  String body = String(s.mac ? s.mac : "") + "|" + (s.boot_id ? s.boot_id : "") +
                "|" + (s.fw_version ? s.fw_version : FW_VERSION);
  String resp;
  int code = uplink_post("/api/v1/commands/claim", body, "text/plain", &resp,
                         CLAIM_HTTP_TIMEOUT_MS);
  if (code == UPLINK_NO_WIFI) return false;
  if (code == 401) {
    if (!g_token_rejected) {
      g_token_rejected = true;
      Serial.println("[cmd] 领取被拒 401：secrets.h 里的 INGEST_TOKEN 与服务端 .env 不一致，"
                     "远程指令通道不可用（连续采集不受影响，因为它用同一把令牌……"
                     "若连续采集也在失败，说明令牌确实错了）");
    }
    return false;
  }
  g_token_rejected = false;
  if (code != 200) {
    Serial.printf("[cmd] 领取失败 HTTP %d resp=%s\n", code, resp.c_str());
    return false;
  }
  resp.trim();
  if (resp == "none" || !resp.length()) return false;
  if (!parse_claim(resp, out)) {
    // 服务端发来了看不懂的东西：如实说出来，不要静默丢弃。
    // 这条指令会在服务端按 timeout 走成 timeout，界面上看得见。
    Serial.printf("[cmd] 领取应答解析失败，原样打印：%s\n", resp.c_str());
    return false;
  }
  return true;
}
// ------------------------------------------------------------------ 上报工具
// add_* 系列一律"先补逗号再写键"，可调用方都是先写一个 "{" 再开始加字段，
// 于是拼出来是 `{,"uptime_s":...` —— 开头多一个逗号，整条 JSON 非法。
// 真机联调时 ping/selftest/capture 的 done 回执就是这样被服务端 422 拒掉的：
// 指令永远停在 running，最后只能等成 timeout，而设备侧还在反复重领同一条，
// 把后面的新指令全堵死。修在 helper 里而不是每个调用点，是因为调用点有十几处，
// 漏一处就是同样的静默失败。已经有内容且不以 "{" 结尾时才需要逗号。
static void jsep(String &j) {
  if (j.length() && !j.endsWith("{")) j += ",";
}

static void add_num(String &j, const char *key, const char *fmt, double v) {
  char b[32];
  snprintf(b, sizeof(b), fmt, v);
  jsep(j);
  j += "\"";
  j += key;
  j += "\":";
  j += b;
}

static void add_bool(String &j, const char *key, bool v) {
  jsep(j);
  j += "\"";
  j += key;
  j += "\":";
  j += v ? "true" : "false";
}

static void add_str(String &j, const char *key, const char *v) {
  jsep(j);
  j += "\"";
  j += key;
  j += "\":\"";
  j += json_escape(v ? v : "");
  j += "\"";
}

static void add_u32(String &j, const char *key, uint32_t v) {
  char b[16];
  snprintf(b, sizeof(b), "%lu", (unsigned long)v);
  jsep(j);
  j += "\"";
  j += key;
  j += "\":";
  j += b;
}

// 结果里的浮点：NaN 必须写成 null。写成 nan 会让整条 JSON 非法，
// 于是"设备报了详细结果"变成"服务端 422"，真正的原因反而看不见了。
static void add_float_or_null(String &j, const char *key, float v, int dec) {
  jsep(j);
  j += "\"";
  j += key;
  j += "\":";
  if (isnan(v)) {
    j += "null";
  } else {
    char b[24];
    snprintf(b, sizeof(b), "%.*f", dec, (double)v);
    j += b;
  }
}

static bool post_done(const char *request_id, const String &result_json,
                      long n_samples, long samples_batch_id) {
  DeviceSnapshot s = snap();
  String j = result_open("done", s);
  j += ",\"progress\":100";
  if (n_samples >= 0) {
    // 设备自报的数量。服务端另有"实际入库数"一列，两者不一致会被标成
    // sample_count_mismatch —— 上传途中掉了多少，不靠猜。
    char b[24];
    snprintf(b, sizeof(b), "%ld", n_samples);
    j += ",\"n_samples\":";
    j += b;
  }
  if (samples_batch_id > 0) {
    char b[24];
    snprintf(b, sizeof(b), "%ld", samples_batch_id);
    j += ",\"samples_batch_id\":";
    j += b;
  }
  if (s.has_device_time) {
    char b[24];
    snprintf(b, sizeof(b), "%llu", (unsigned long long)s.t_device_ntp_ms);
    j += ",\"t_device_ms\":";
    j += b;
  }
  if (result_json.length()) {
    j += ",\"result\":";
    j += result_json;
  }
  j += "}";
  post_terminal(request_id, j);
  Serial.printf("[cmd] 已回报成功 req=%s\n", request_id);
  return true;
}

// ------------------------------------------------------------------ ping
static void run_ping(const Command &c) {
  DeviceSnapshot s = snap();
  String r;
  r.reserve(384);
  r += "{";
  add_u32(r, "uptime_s", millis() / 1000);
  add_num(r, "rssi_dbm", "%.0f", (double)WiFi.RSSI());
  add_u32(r, "free_heap", ESP.getFreeHeap());
  add_u32(r, "min_free_heap", ESP.getMinFreeHeap());
  add_u32(r, "ring_count", s.ring_count);
  add_u32(r, "ring_capacity", s.ring_capacity);
  add_u32(r, "stream_seq", s.stream_seq);
  add_u32(r, "dropped_since_last", s.dropped_since_last);
  add_bool(r, "collect_enabled", s.collect_enabled);
  add_bool(r, "ntp_synced", s.ntp_synced);
  if (s.ntp_synced) add_u32(r, "ntp_sync_age_s", s.ntp_age_s);
  else r += ",\"ntp_sync_age_s\":null";
  add_str(r, "wifi_ip", WiFi.localIP().toString().c_str());
  add_str(r, "fw_version", s.fw_version ? s.fw_version : FW_VERSION);
  add_u32(r, "sample_interval_ms", SAMPLE_INTERVAL_MS);
  add_u32(r, "upload_interval_ms", UPLOAD_INTERVAL_MS);
  add_u32(r, "command_poll_ms", COMMAND_POLL_MS);
  r += "}";
  post_done(c.request_id, r, -1, -1);
}
// ------------------------------------------------------------------ selftest
static void run_selftest(const Command &c) {
  uint32_t accel_ok = 0, mic_ok = 0;
  double mag_sum = 0;
  uint32_t mag_n = 0;
  float mag_min = 1e9f, mag_max = -1e9f;
  double spl_sum = 0;
  uint32_t spl_n = 0;
  float spl_min = 1e9f, spl_max = -1e9f;

  for (int i = 0; i < SELFTEST_ACCEL_READS; i++) {
    float ax, ay, az;
    if (accel_read(&ax, &ay, &az)) {
      accel_ok++;
      float m = sqrtf(ax * ax + ay * ay + az * az);
      mag_sum += m;
      mag_n++;
      if (m < mag_min) mag_min = m;
      if (m > mag_max) mag_max = m;
    }
    idle();                 // 自检期间连续流照常跑
    delay(5);
  }
  for (int i = 0; i < SELFTEST_MIC_READS; i++) {
    float spl = NAN;
    if (mic_read_spl(&spl) && !isnan(spl)) {
      mic_ok++;
      spl_sum += spl;
      spl_n++;
      if (spl < spl_min) spl_min = spl;
      if (spl > spl_max) spl_max = spl;
    } else {
      idle();               // 麦克风读失败时不会消耗 DMA 数据，让主循环有机会跑
      delay(5);
    }
  }

  float mag_avg = mag_n ? (float)(mag_sum / mag_n) : NAN;
  float spl_avg = spl_n ? (float)(spl_sum / spl_n) : NAN;
  MicStatus ms = mic_last_status();

  // 判据分两层，两层都写进结果：
  //   读得到吗（通信层）      —— accel_ok / mic_ok 的命中数
  //   读到的说得通吗（物理层）—— |a| 是否接近 1 g（板子静止这个前提是自检时成立的）
  bool accel_verdict = accel_ok >= (uint32_t)(SELFTEST_ACCEL_READS * SELFTEST_MIN_OK_RATIO) &&
                       mag_n && fabsf(mag_avg - 1.0f) <= SELFTEST_MAG_TOL;
  bool mic_verdict = mic_ok >= (uint32_t)(SELFTEST_MIC_READS * SELFTEST_MIN_OK_RATIO) &&
                     ms.frames_read > 0;

  String r;
  r.reserve(420);
  r += "{";
  add_u32(r, "accel_reads", SELFTEST_ACCEL_READS);
  add_u32(r, "accel_ok_reads", accel_ok);
  add_float_or_null(r, "mag_avg", mag_avg, 4);
  add_float_or_null(r, "mag_min", mag_n ? mag_min : NAN, 4);
  add_float_or_null(r, "mag_max", mag_n ? mag_max : NAN, 4);
  add_num(r, "mag_tol", "%.2f", SELFTEST_MAG_TOL);
  add_bool(r, "accel_verdict", accel_verdict);
  add_u32(r, "mic_reads", SELFTEST_MIC_READS);
  add_u32(r, "mic_ok_reads", mic_ok);
  add_u32(r, "mic_frames_last", ms.frames_read);
  add_float_or_null(r, "spl_avg_db", spl_avg, 1);
  add_float_or_null(r, "spl_min_db", spl_n ? spl_min : NAN, 1);
  add_float_or_null(r, "spl_max_db", spl_n ? spl_max : NAN, 1);
  add_bool(r, "mic_verdict", mic_verdict);
  add_bool(r, "mic_driver_ok", ms.ok);
  r += "}";

  if (accel_verdict && mic_verdict) {
    post_done(c.request_id, r, -1, -1);
    return;
  }
  // 失败要报"哪一路、为什么"，不能只给一个 false。
  const char *code = !accel_ok ? "accel_i2c"
                     : !mic_ok ? "mic_no_output"
                     : !accel_verdict ? "accel_out_of_range"
                                      : "mic_out_of_range";
  char mag_txt[16];
  snprintf(mag_txt, sizeof(mag_txt), mag_n ? "%.3f" : "nan", (double)mag_avg);
  char msg[160];
  snprintf(msg, sizeof(msg),
           "accel %lu/%d 次可读 |a|=%s；mic %lu/%d 次可读 frames=%lu",
           (unsigned long)accel_ok, SELFTEST_ACCEL_READS, mag_txt,
           (unsigned long)mic_ok, SELFTEST_MIC_READS,
           (unsigned long)ms.frames_read);
  post_failed(c.request_id, code, msg, r);
}

// ------------------------------------------------------------------ capture
static void run_capture(const Command &c) {
  static Sample buf[MAX_BATCH];     // 100 * 24B = 2.4KB，放 .bss 不占栈
  uint16_t buf_n = 0;

  const long n_req = c.n;
  const uint32_t interval = (uint32_t)c.interval_ms;
  // 预算 = 服务端给的 timeout 减去回执余量。到点就收手并如实报 deadline_exceeded，
  // 绝不"再多采一会儿"：那样服务端已经判了超时，板子还在采，两边结论对不上。
  const uint32_t budget = c.timeout_ms > CAPTURE_MARGIN_MS
                              ? c.timeout_ms - CAPTURE_MARGIN_MS
                              : c.timeout_ms / 2;

  uint32_t n_sampled = 0, n_accel_fail = 0, n_mic_fail = 0;
  uint32_t n_acked = 0, n_upload_failed = 0;
  double spl_sum = 0, mag_sum = 0;
  uint32_t spl_n = 0, mag_n = 0;
  float spl_min = 1e9f, spl_max = -1e9f;
  long last_batch_id = -1;
  bool deadline_hit = false;
  int last_pct = -1;

  post_progress(c.request_id, 0);

  auto flush = [&]() {
    if (buf_n == 0) return;
    DeviceSnapshot s = snap();
    DeviceId id = {s.mac, s.boot_id, s.fw_version ? s.fw_version : FW_VERSION};
    BatchMeta meta = {};
    meta.ntp_synced = s.ntp_synced;
    meta.ntp_age_s = s.ntp_age_s;
    meta.has_device_time = s.has_device_time;
    meta.t_device_ntp_ms = s.t_device_ntp_ms;
    // 恒为 0：dropped_since_last 记的是"连续流缓冲溢出"，那份账由 main 在
    // 下一次连续流上传时结清。指令上传再报一次就等于同一笔丢弃被记两遍。
    meta.dropped_since_last = 0;
    meta.request_id = c.request_id;
    long bid = -1;
    if (uplink_ingest(id, meta, buf, buf_n, &bid)) {
      n_acked += buf_n;
      if (bid > 0) last_batch_id = bid;
    } else {
      n_upload_failed += buf_n;
    }
    buf_n = 0;
  };

  const uint32_t t_start = millis();
  for (long i = 0; i < n_req; i++) {
    // 用绝对时刻排采样点，而不是"每次 delay(interval)"：
    // 后者会把读传感器和上传的耗时累加进去，实际间隔越来越长，
    // 采出来的数据就不再是"每 interval 毫秒一个点"。
    const uint32_t target = t_start + (uint32_t)i * interval;
    const int32_t wait = (int32_t)(target - millis());
    if (wait > 0) idle_for((uint32_t)wait);

    if (millis() - t_start > budget) {
      deadline_hit = true;
      Serial.printf("[cmd] req=%s 预算 %lums 用尽，已采 %lu/%ld，提前收手\n",
                    c.request_id, (unsigned long)budget, (unsigned long)n_sampled,
                    n_req);
      break;
    }

    Sample sm = {};
    sm.seq = (uint32_t)i;             // 指令流自己的 seq，从 0 起算
    sm.t_device_ms = millis();
    float spl = NAN;
    bool a_ok = accel_read(&sm.ax, &sm.ay, &sm.az);
    bool m_ok = mic_read_spl(&spl);
    sm.spl_db = spl;
    if (!a_ok) {
      // 读不到就写 NaN（上传时成 null），绝不留下 0.000 冒充"静止"。
      // 0 g 是一个物理上说得通的值，混进数据里没人能发现——这正是最坏的失败方式。
      n_accel_fail++;
      sm.ax = sm.ay = sm.az = NAN;
    } else {
      float m = sqrtf(sm.ax * sm.ax + sm.ay * sm.ay + sm.az * sm.az);
      mag_sum += m;
      mag_n++;
    }
    if (!m_ok || isnan(spl)) n_mic_fail++;
    else {
      spl_sum += spl;
      spl_n++;
      if (spl < spl_min) spl_min = spl;
      if (spl > spl_max) spl_max = spl;
    }
    if (a_ok || m_ok) {
      buf[buf_n++] = sm;
      n_sampled++;
    }
    if (buf_n >= MAX_BATCH) flush();

    const int pct = (int)((i + 1) * 100 / n_req);
    if (pct / 25 > last_pct / 25 && pct < 100) {   // 每越过 25% 报一次
      last_pct = pct;
      post_progress(c.request_id, pct);
    } else if (pct > last_pct) {
      last_pct = pct;
    }
  }
  flush();

  const uint32_t duration = millis() - t_start;
  String r;
  r.reserve(480);
  r += "{";
  add_num(r, "n_requested", "%.0f", (double)n_req);
  add_u32(r, "n_sampled", n_sampled);
  add_u32(r, "n_accel_fail", n_accel_fail);
  add_u32(r, "n_mic_fail", n_mic_fail);
  add_u32(r, "n_acked", n_acked);
  add_u32(r, "n_upload_failed", n_upload_failed);
  add_u32(r, "interval_ms", interval);
  add_u32(r, "duration_ms", duration);
  add_bool(r, "deadline_exceeded", deadline_hit);
  add_float_or_null(r, "spl_avg_db", spl_n ? (float)(spl_sum / spl_n) : NAN, 1);
  add_float_or_null(r, "spl_min_db", spl_n ? spl_min : NAN, 1);
  add_float_or_null(r, "spl_max_db", spl_n ? spl_max : NAN, 1);
  add_float_or_null(r, "mag_avg", mag_n ? (float)(mag_sum / mag_n) : NAN, 4);
  if (last_batch_id > 0) add_num(r, "last_batch_id", "%.0f", (double)last_batch_id);
  else r += ",\"last_batch_id\":null";
  r += "}";

  // 错误码按"损失最重的先报"排：数据没到服务端 > 传感器根本没读出来 > 没采完。
  if (n_upload_failed > 0) {
    char msg[128];
    snprintf(msg, sizeof(msg), "%lu 个样本上传未获确认（已确认 %lu）",
             (unsigned long)n_upload_failed, (unsigned long)n_acked);
    post_failed(c.request_id, "upload_failed", msg, r);
  } else if (n_sampled == 0) {
    post_failed(c.request_id, n_accel_fail ? "accel_i2c" : "sensor_read_failed",
                "一个样本都没采到：加速度计与麦克风均无有效输出", r);
  } else if (deadline_hit) {
    char msg[128];
    snprintf(msg, sizeof(msg), "预算 %lums 内只采到 %lu/%ld",
             (unsigned long)budget, (unsigned long)n_sampled, n_req);
    post_failed(c.request_id, "deadline_exceeded", msg, r);
  } else if (n_accel_fail == (uint32_t)n_req) {
    post_failed(c.request_id, "accel_i2c", "全部采样的加速度读取均失败", r);
  } else if (n_mic_fail == (uint32_t)n_req) {
    post_failed(c.request_id, "mic_no_output", "全部采样的麦克风读取均失败", r);
  } else {
    // 部分通道有失败也算成功，但失败次数原样写在结果里。
    // 判"成功"只看"数据有没有按请求采到并入库"，瑕疵由数字自己说话。
    post_done(c.request_id, r, (long)n_acked, last_batch_id);
  }
}

// ------------------------------------------------------------------ 调度
static void run_command(const Command &c) {
  g_executing = true;
  // 执行前就记下，而不是执行后：万一中途复位，幂等重领时也不会再跑一遍。
  strncpy(g_done_rid, c.request_id, sizeof(g_done_rid) - 1);
  g_done_rid[sizeof(g_done_rid) - 1] = '\0';
  Serial.printf("[cmd] 领取 req=%s op=%s n=%ld interval=%ldms timeout=%lums\n",
                c.request_id, c.op, c.n, c.interval_ms,
                (unsigned long)c.timeout_ms);
  if (strcmp(c.op, "ping") == 0)            run_ping(c);
  else if (strcmp(c.op, "selftest") == 0)   run_selftest(c);
  else if (strcmp(c.op, "capture") == 0)    run_capture(c);
  else {
    // 服务端加了新 op 而固件没更新：报 unsupported_op，让界面上出现一条明确
    // 的失败，而不是让用户盯着"执行中"等到超时——那种失败看不出是固件太旧。
    char msg[96];
    snprintf(msg, sizeof(msg), "固件 " FW_VERSION " 不支持 op=%s，请升级固件", c.op);
    post_failed(c.request_id, "unsupported_op", msg, String());
  }
  g_executing = false;
}

void command_begin(SnapshotFn s, IdleFn idle_fn) {
  g_snap = s;
  g_idle = idle_fn;
  g_last_poll_ms = 0;
  Serial.printf("[cmd] 远程指令通道就绪，每 %d ms 领取一次\n", COMMAND_POLL_MS);
}

void command_poll() {
  if (!g_snap) return;                       // 没接线就什么都不做，不猜
  const uint32_t now = millis();
  if (g_last_poll_ms != 0 && now - g_last_poll_ms < COMMAND_POLL_MS) return;
  g_last_poll_ms = now;

  // 欠着终态回执就先把账还上，这一轮不去领新活——领了也执行不了。
  if (g_pend_body.length()) {
    if (post_result_to(g_pend_rid, g_pend_body)) {
      Serial.printf("[cmd] 补发终态回执成功 req=%s\n", g_pend_rid);
      g_pend_body = String();
      g_pend_rid[0] = '\0';
      g_pend_tries = 0;
    } else if (++g_pend_tries >= PEND_RETRY_MAX) {
      // 放弃，但不装作没事：说清楚是哪条、交给服务端判 timeout。
      Serial.printf("[cmd] 终态回执补发 %u 次仍失败，放弃 req=%s，"
                    "该指令将由服务端判为 timeout\n",
                    (unsigned)g_pend_tries, g_pend_rid);
      g_pend_body = String();
      g_pend_rid[0] = '\0';
      g_pend_tries = 0;
    }
    return;
  }

  Command c;
  if (!try_claim(c)) return;
  if (strcmp(c.request_id, g_done_rid) == 0) {
    // 幂等重领：这条已经跑过了。重跑会重复采集、重复入库，还会把服务端的
    // 静默计时一直刷新，让指令永远停在 running。什么都不做，等它自己终结。
    return;
  }
  run_command(c);
}

bool command_executing() { return g_executing; }