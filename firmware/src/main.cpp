// ESP32-S3-EYE 遥测节点
//
// 设计要点（对应"数据来源可追溯 / 时间戳可信度明确 / 失败必须可见"）：
//   - boot_id：每次启动随机生成，重启在服务端是可见事件，不会被当成连续数据
//   - seq：本次启动内自增。同一 boot_id 内 seq 不连续即板上确有丢样
//   - dropped_since_last：上传失败导致环形缓冲溢出而丢弃的样本数，
//     随下一次成功上传上报，因此"失败"不会静默消失
//   - NTP 状态：上报是否同步过、同步了多久，让服务端能判断设备时间可不可信
//   - 持续采样 => 数据流本身就是心跳。界面安静即代表设备失联，
//     而不是"没有事件发生"
//
// 第2周新增：Web 远程采集指令。实现全部在 command.cpp / uplink.cpp 里，
// 本文件只做三件接线的事：把设备身份与状态"现场取值"的回调交出去、
// 把连续流节拍抽成 stream_tick() 供指令执行期间复用、在 loop 里调 command_poll()。
// 之所以不让指令模块自己持有一份状态副本：同一个事实存两处必然漂移。

#include <Arduino.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WiFi.h>
#include <esp_sntp.h>
#include <time.h>

#include "command.h"   // 第2周：远程指令的领取与执行（独立模块，本文件只接线）
#include "config.h"
#include "secrets.h"
#include "sensors.h"
#include "uplink.h"    // HTTP 上行统一出口（ingest / claim / result 共用一份超时与令牌）

static const uint32_t NTP_VALID_EPOCH = 1600000000UL; // 2020-09-13，早于此视为未同步

// ---- 环形缓冲 ----
static Sample g_ring[RING_CAPACITY];
static uint16_t g_head = 0; // 下一个写入位置
static uint16_t g_count = 0;
static uint32_t g_dropped_since_last = 0;

// ---- 身份与序号 ----
static char g_boot_id[9];
static char g_mac[18];
static uint32_t g_seq = 0;

// ---- 时间状态 ----
static bool g_ntp_synced = false;
static uint32_t g_ntp_sync_uptime_ms = 0;
static uint32_t g_ntp_sync_epoch_s = 0;
static uint32_t g_last_ntp_request = 0;

static Preferences g_prefs;
static uint32_t g_dropped_lifetime = 0;

// ---- 采集开关 ----
// 默认"开"。取不到服务端状态时维持现状，不因为一次网络抖动就自行停采——
// 那种停止在界面上没有任何痕迹，正是本项目最该避免的失败方式。
static bool g_collect = true;

static void make_boot_id() {
  uint32_t r = esp_random();
  snprintf(g_boot_id, sizeof(g_boot_id), "%08X", r);
}

static void ring_push(const Sample &s) {
  if (g_count == RING_CAPACITY) {
    // 缓冲满 = 上传一直在失败。丢最旧的并记账，不能假装没发生。
    g_head = (g_head + 1) % RING_CAPACITY;
    g_count--;
    g_dropped_since_last++;
    g_dropped_lifetime++;
  }
  uint16_t idx = (g_head + g_count) % RING_CAPACITY;
  g_ring[idx] = s;
  g_count++;
}

// 仅当上传成功后才移除，避免上传失败时数据被吞掉
static void ring_drop_front(uint16_t n) {
  if (n > g_count) n = g_count;
  g_head = (g_head + n) % RING_CAPACITY;
  g_count -= n;
}

static uint32_t ntp_age_s() {
  if (!g_ntp_synced) return 0;
  return (millis() - g_ntp_sync_uptime_ms) / 1000;
}

static bool device_time_ms(uint64_t *out) {
  if (!g_ntp_synced) return false;
  *out = (uint64_t)g_ntp_sync_epoch_s * 1000ULL +
         (millis() - g_ntp_sync_uptime_ms);
  return true;
}

static void wifi_ensure() {
  if (WiFi.status() == WL_CONNECTED) return;
  static uint32_t last_try = 0;
  if (millis() - last_try < 10000 && last_try != 0) return;
  last_try = millis();
  Serial.println("[wifi] 重连中 ...");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

// 只有真的收到 SNTP 应答，lwIP 才会调用这个回调——这正是"同步年龄"该有的口径。
//
// 不能用"configTime() 之后读一次 time() 是否合理"来判断同步成功：系统时钟本来就
// 从上一次同步起一直在走，那个值必然合理，于是每次重同步都会把年龄清零，
// 把"没有收到任何应答"说成"刚刚同步过"。即"新鲜"这个结论本身不可信。
static void on_sntp_sync(struct timeval *tv) {
  if (tv->tv_sec < (time_t)NTP_VALID_EPOCH) return; // 明显不合理的值不采信
  g_ntp_sync_uptime_ms = millis();
  g_ntp_sync_epoch_s = (uint32_t)tv->tv_sec;
  g_ntp_synced = true;
  Serial.printf("[ntp] 收到应答 epoch=%lu\n", (unsigned long)tv->tv_sec);
}

static void ntp_ensure() {
  static bool cb_ready = false;
  if (!cb_ready) {
    cb_ready = true;
    sntp_set_time_sync_notification_cb(on_sntp_sync);
  }

  bool need = !g_ntp_synced || (millis() - g_ntp_sync_uptime_ms > NTP_RESYNC_MS);
  if (!need) return;
  if (g_last_ntp_request != 0 && millis() - g_last_ntp_request < 10000) return;
  g_last_ntp_request = millis();

  if (!g_ntp_synced) {
    Serial.println("[ntp] 首次同步 ...");
    configTime(8 * 3600, 0, "ntp.aliyun.com", "ntp.tencent.com", "pool.ntp.org");
  } else {
    // 已有过一次同步：不清空状态、不重置年龄，只强制重新查询。
    // 查询成功时由回调把年龄清零；一直失败就让它如实增长直到越过阈值。
    sntp_restart();
  }
}

// 轮询服务端的采集开关。返回体刻意是纯文本 "1"/"0"：只有一个布尔值，
// 不值得为它引入 JSON 解析。
// 只有明确读到 "0" 才停；其余情况（超时、非 200、内容不认识）一律当"继续采集"。
static void control_poll() {
  static uint32_t last = 0;
  if (last != 0 && millis() - last < CONTROL_POLL_MS) return;
  last = millis();
  if (WiFi.status() != WL_CONNECTED) return;

  HTTPClient http;
  http.setTimeout(2000);
  http.setConnectTimeout(2000);
  if (!http.begin(String(SERVER_URL) + "/api/v1/control/state")) return;
  int code = http.GET();
  if (code == 200) {
    String body = http.getString();
    body.trim();
    bool want = (body != "0");
    if (want != g_collect) {
      Serial.printf("[ctl] 采集 %s -> %s\n", g_collect ? "开" : "停",
                    want ? "开" : "停（缓冲区里已采到的样本仍会传完）");
      g_collect = want;
    }
  }
  http.end();
}

static DeviceId device_id() {
  DeviceId id = {g_mac, g_boot_id, FW_VERSION};
  return id;
}

static BatchMeta stream_meta() {
  BatchMeta m = {};
  m.ntp_synced = g_ntp_synced;
  m.ntp_age_s = ntp_age_s();
  m.has_device_time = device_time_ms(&m.t_device_ntp_ms);
  m.dropped_since_last = g_dropped_since_last;
  m.request_id = nullptr;   // nullptr = 连续流样本；指令采集的样本由 command.cpp 带上 request_id
  return m;
}

// uplink 只接受连续数组，而环形缓冲会绕回，所以先拷出来。
// 放 static：100 * 24B = 2.4KB，压在 loop 任务的栈上不划算。
static Sample g_batch_buf[MAX_BATCH];

// 返回 true 表示服务端已确认收下这批数据
static bool upload_batch(uint16_t n) {
  if (n == 0) return false;
  if (n > MAX_BATCH) n = MAX_BATCH;
  for (uint16_t i = 0; i < n; i++)
    g_batch_buf[i] = g_ring[(g_head + i) % RING_CAPACITY];
  return uplink_ingest(device_id(), stream_meta(), g_batch_buf, n);
}

// 指令模块要的"设备此刻的样子"。全部现场取值，不在那边存副本。
static void fill_snapshot(DeviceSnapshot *out) {
  out->mac = g_mac;
  out->boot_id = g_boot_id;
  out->fw_version = FW_VERSION;
  out->ntp_synced = g_ntp_synced;
  out->ntp_age_s = ntp_age_s();
  out->has_device_time = device_time_ms(&out->t_device_ntp_ms);
  out->dropped_since_last = g_dropped_since_last;
  out->stream_seq = g_seq;
  out->ring_count = g_count;
  out->ring_capacity = RING_CAPACITY;
  out->collect_enabled = g_collect;
}

// 一次"连续流"节拍：采样与上传各有自己的时间闸门，不到点就什么都不做。
//
// 抽成函数是第2周的需要：远程 capture 最长要跑 10 秒，那段时间如果主循环
// 被独占，连续流就会出现一个 10 秒的断层，界面上画成红色"上传中断"竖带——
// 把用户自己点的按钮显示成设备故障。所以指令执行期间会回调这里。
static void stream_tick() {
  uint32_t now = millis();
  static uint32_t last_sample = 0;
  static uint32_t last_upload = 0;

  // 停止采集只停"采样"，不停"上传"：缓冲区里已经采到的样本要照常传完。
  // 按一次停止就丢掉手上已有数据，等于让用户的操作毁掉数据。
  if (g_collect && now - last_sample >= SAMPLE_INTERVAL_MS) {
    last_sample = now;
    Sample s = {};
    s.seq = g_seq++;
    s.t_device_ms = now;
    float spl = NAN;
    bool accel_ok = accel_read(&s.ax, &s.ay, &s.az);
    // 读不到就写 NaN（入库为 null），不要留下 0.000：
    // 0 g 是物理上说得通的值，混进数据里没人能发现，这是最坏的失败方式。
    if (!accel_ok) s.ax = s.ay = s.az = NAN;
    bool mic_ok = mic_read_spl(&spl);
    s.spl_db = spl;
    if (accel_ok || mic_ok) {
      ring_push(s);
    } else {
      g_dropped_since_last++;
    }
  }

  if (now - last_upload >= UPLOAD_INTERVAL_MS) {
    last_upload = now;
    uint16_t n = g_count > MAX_BATCH ? MAX_BATCH : g_count;
    if (n > 0) {
      if (upload_batch(n)) {
        Serial.printf("[uplink] OK 样本=%u 剩余=%u 本次丢=%lu 累计丢=%lu\n", n,
                      g_count - n, (unsigned long)g_dropped_since_last,
                      (unsigned long)g_dropped_lifetime);
        g_dropped_lifetime += g_dropped_since_last;
        g_prefs.putULong("dropped", g_dropped_lifetime);
        g_dropped_since_last = 0;
        ring_drop_front(n);
      } else if (g_count >= RING_CAPACITY) {
        Serial.println("[uplink] 持续失败，缓冲已满开始丢弃");
      }
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(1500);

  Serial.println("\n\n===== ESP32-S3-EYE 遥测节点 =====");
  make_boot_id();
  uint8_t mac[6];
  WiFi.macAddress(mac);
  snprintf(g_mac, sizeof(g_mac), "%02X:%02X:%02X:%02X:%02X:%02X", mac[0],
           mac[1], mac[2], mac[3], mac[4], mac[5]);
  Serial.printf("boot_id=%s  mac=%s  fw=" FW_VERSION "\n", g_boot_id, g_mac);

  g_prefs.begin("telemetry", false);
  g_dropped_lifetime = g_prefs.getULong("dropped", 0);
  Serial.printf("历史累计丢弃样本 = %lu\n", (unsigned long)g_dropped_lifetime);

  Serial.printf("[wifi] 连接 \"%s\" ...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
    delay(300);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[wifi] 已连接  IP=%s  RSSI=%d dBm\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
  } else {
    Serial.println("[wifi] 20 秒内未连上，继续后台重试");
  }

  ntp_ensure();

  if (!sensors_begin()) {
    Serial.println("[sensors] 有传感器不可用，仍将继续运行并上报可用的那部分");
  }

  // 第2周接线：把"现场取值"和"连续流节拍"两个回调交给指令模块。
  // 放在 sensors_begin 之后：自检/采集要用的传感器此时才真正就绪。
  command_begin(fill_snapshot, stream_tick);
  Serial.printf("[cfg] 服务端=%s  采样=%dms  上传=%dms\n", SERVER_URL,
                SAMPLE_INTERVAL_MS, UPLOAD_INTERVAL_MS);
  Serial.println("===== 开始采集 =====\n");
}

void loop() {
  wifi_ensure();
  if (WiFi.status() == WL_CONNECTED) {
    ntp_ensure();
    control_poll();
    // 第2周：领取并执行一条远程指令（内部按 COMMAND_POLL_MS 节流）。
    // 一次只领一条、执行完再领下一条：板上不存指令队列，
    // 掉线重启后由服务端按超时重新入队或判超时，避免两个事实来源。
    command_poll();
  }
  stream_tick();
}
