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

#include <Arduino.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WiFi.h>
#include <esp_sntp.h>
#include <time.h>

#include "config.h"
#include "secrets.h"
#include "sensors.h"

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

static String build_payload(uint16_t n) {
  String j;
  j.reserve(256 + n * 110);
  uint64_t tdev = 0;
  bool has_dev_time = device_time_ms(&tdev);

  j += "{\"device_mac\":\"";
  j += g_mac;
  j += "\",\"boot_id\":\"";
  j += g_boot_id;
  j += "\",\"fw_version\":\"" FW_VERSION "\"";
  j += ",\"ntp_synced\":";
  j += g_ntp_synced ? "true" : "false";
  j += ",\"ntp_sync_age_s\":";
  j += g_ntp_synced ? String(ntp_age_s()) : "null";
  j += ",\"t_device_ntp_ms\":";
  if (has_dev_time) {
    // epoch 毫秒约 1.79e12，必须按 64 位格式化；转成 unsigned long(32位) 会截断
    char tbuf[24];
    snprintf(tbuf, sizeof(tbuf), "%llu", (unsigned long long)tdev);
    j += tbuf;
  } else {
    j += "null";
  }
  j += ",\"dropped_since_last\":";
  j += String(g_dropped_since_last);
  j += ",\"readings\":[";

  for (uint16_t i = 0; i < n; i++) {
    const Sample &s = g_ring[(g_head + i) % RING_CAPACITY];
    if (i) j += ",";
    char buf[128];
    char spl[16];
    if (isnan(s.spl_db))
      snprintf(spl, sizeof(spl), "null");
    else
      snprintf(spl, sizeof(spl), "%.1f", s.spl_db);
    snprintf(buf, sizeof(buf),
             "{\"seq\":%lu,\"t_device_ms\":%lu,\"ax\":%.3f,\"ay\":%.3f,"
             "\"az\":%.3f,\"spl_db\":%s}",
             (unsigned long)s.seq, (unsigned long)s.t_device_ms, s.ax, s.ay,
             s.az, spl);
    j += buf;
  }
  j += "]}";
  return j;
}

// 返回 true 表示服务端已确认收下这批数据
static bool upload_batch(uint16_t n) {
  if (WiFi.status() != WL_CONNECTED) return false;

  String payload = build_payload(n);
  HTTPClient http;
  http.setTimeout(5000);
  http.setConnectTimeout(3000);
  String url = String(SERVER_URL) + "/api/v1/ingest";
  if (!http.begin(url)) {
    Serial.println("[uplink] http.begin 失败");
    return false;
  }
  http.addHeader("Content-Type", "application/json");
  // 服务端未配置 INGEST_TOKEN 时不校验，带上也无害；配置了就必须要配对
  http.addHeader("X-Ingest-Token", INGEST_TOKEN);
  int code = http.POST(payload);
  String body = http.getString();
  http.end();

  if (code != 201) {
    Serial.printf("[uplink] 上传失败 HTTP %d  样本=%u\n", code, n);
    return false;
  }
  return true;
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
  Serial.printf("[cfg] 服务端=%s  采样=%dms  上传=%dms\n", SERVER_URL,
                SAMPLE_INTERVAL_MS, UPLOAD_INTERVAL_MS);
  Serial.println("===== 开始采集 =====\n");
}

void loop() {
  uint32_t now = millis();

  wifi_ensure();
  if (WiFi.status() == WL_CONNECTED) ntp_ensure();

  static uint32_t last_sample = 0;
  static uint32_t last_upload = 0;

  if (now - last_sample >= SAMPLE_INTERVAL_MS) {
    last_sample = now;
    Sample s = {};
    s.seq = g_seq++;
    s.t_device_ms = now;
    float spl = NAN;
    bool accel_ok = accel_read(&s.ax, &s.ay, &s.az);
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
        Serial.printf("[uplink] 持续失败，缓冲已满开始丢弃\n");
      }
    }
  }
}
