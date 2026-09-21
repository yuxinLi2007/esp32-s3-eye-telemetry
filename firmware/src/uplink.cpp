#include "uplink.h"

#include <HTTPClient.h>
#include <WiFi.h>

#include "config.h"
#include "secrets.h"

// 断网熔断：连续失败后短时间内不再发起新请求。
// 否则一次断网会把主循环每一轮都用连接超时堵满——真机实测采样节拍被从
// 2s 拖到 8s，连本地按键反馈都跟着卡（那是另一个 bug，已在 button.cpp 修）。
// 退避 2s 起、失败翻倍、封顶 8s；任何一次成功立刻复位。
static uint32_t g_down_until = 0;
static uint32_t g_backoff_ms = 0;

int uplink_post(const char *path, const String &body, const char *content_type,
                String *resp, uint32_t timeout_ms) {
  if (WiFi.status() != WL_CONNECTED) return UPLINK_NO_WIFI;

  // 熔断窗口内直接返回"没发出去"，调用方的失败处理与真断网完全一致：
  // 事件留在队列里等恢复，不丢也不静默。
  if (g_down_until != 0 && (int32_t)(millis() - g_down_until) < 0) {
    return UPLINK_NO_WIFI;
  }

  HTTPClient http;
  http.setTimeout(timeout_ms);
  http.setConnectTimeout(1500);
  String url = String(SERVER_URL) + path;
  if (!http.begin(url)) {
    Serial.printf("[uplink] http.begin 失败 url=%s\n", url.c_str());
    return UPLINK_BEGIN_FAILED;
  }
  http.addHeader("Content-Type", content_type);
  // 服务端未配置 INGEST_TOKEN 时不校验，带上也无害；配置了就必须要配对。
  // 指令的领取与回执复用同一把令牌：板子上已经配了这一把，不再多配一把，
  // 多一把就多一种配错的可能，而配错的后果是"指令永远发不出去"。
  http.addHeader("X-Ingest-Token", INGEST_TOKEN);

  int code = http.POST(body);
  if (resp) *resp = http.getString();
  http.end();

  if (code <= 0) {
    g_backoff_ms = g_backoff_ms ? (g_backoff_ms * 2 > 8000 ? 8000 : g_backoff_ms * 2) : 2000;
    g_down_until = millis() + g_backoff_ms;
  } else {
    g_backoff_ms = 0;
    g_down_until = 0;
  }
  return code;
}

String json_escape(const char *s) {
  String out;
  if (!s) return out;
  for (const char *p = s; *p; ++p) {
    unsigned char c = (unsigned char)*p;
    switch (c) {
      case '"':  out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n";  break;
      case '\r': out += "\\r";  break;
      case '\t': out += "\\t";  break;
      default:
        if (c < 0x20) {
          char b[8];
          snprintf(b, sizeof(b), "\\u%04x", c);
          out += b;
        } else {
          out += (char)c;   // UTF-8 的多字节序列每个字节都 >= 0x80，原样透传
        }
    }
  }
  return out;
}

// 浮点或 null。NaN 不能进 JSON，见调用处注释。
static void fmt_val(char *out, size_t cap, float v, const char *fmt) {
  if (isnan(v))
    snprintf(out, cap, "null");
  else
    snprintf(out, cap, fmt, (double)v);
}

String uplink_ingest_json(const DeviceId &id, const BatchMeta &meta,
                          const Sample *samples, uint16_t n) {
  String j;
  j.reserve(256 + (size_t)n * 110);

  j += "{\"device_mac\":\"";
  j += id.mac;
  j += "\",\"boot_id\":\"";
  j += id.boot_id;
  j += "\",\"fw_version\":\"";
  j += id.fw_version;
  j += "\"";
  if (meta.request_id && meta.request_id[0]) {
    // 指令采集的样本必须带上 request_id，否则它会混进连续流：
    // 两条流的 seq 各自从 0 计数，混在一起算缺口会报出一堆假丢样。
    j += ",\"request_id\":\"";
    j += meta.request_id;
    j += "\"";
  }
  j += ",\"ntp_synced\":";
  j += meta.ntp_synced ? "true" : "false";
  j += ",\"ntp_sync_age_s\":";
  j += meta.ntp_synced ? String(meta.ntp_age_s) : "null";
  j += ",\"t_device_ntp_ms\":";
  if (meta.has_device_time) {
    // epoch 毫秒约 1.79e12，必须按 64 位格式化；转成 unsigned long(32位) 会截断
    char tbuf[24];
    snprintf(tbuf, sizeof(tbuf), "%llu", (unsigned long long)meta.t_device_ntp_ms);
    j += tbuf;
  } else {
    j += "null";
  }
  j += ",\"dropped_since_last\":";
  j += String(meta.dropped_since_last);
  j += ",\"readings\":[";

  for (uint16_t i = 0; i < n; i++) {
    const Sample &s = samples[i];
    if (i) j += ",";
    // 四个测量值都可能是 NaN（通道读不到）。NaN 必须写成 null：
    // printf 会打出 "nan"，那不是合法 JSON，整批数据会被服务端 422 退回来，
    // 于是"某个通道没读出来"升级成"这一批全丢了"，故障被放大而不是被暴露。
    char buf[160];
    char spl[16], ax[16], ay[16], az[16];
    fmt_val(spl, sizeof(spl), s.spl_db, "%.1f");
    fmt_val(ax, sizeof(ax), s.ax, "%.3f");
    fmt_val(ay, sizeof(ay), s.ay, "%.3f");
    fmt_val(az, sizeof(az), s.az, "%.3f");
    snprintf(buf, sizeof(buf),
             "{\"seq\":%lu,\"t_device_ms\":%lu,\"ax\":%s,\"ay\":%s,"
             "\"az\":%s,\"spl_db\":%s}",
             (unsigned long)s.seq, (unsigned long)s.t_device_ms, ax, ay, az, spl);
    j += buf;
  }
  j += "]}";
  return j;
}

bool uplink_ingest(const DeviceId &id, const BatchMeta &meta,
                   const Sample *samples, uint16_t n, long *batch_id_out) {
  if (batch_id_out) *batch_id_out = -1;
  if (n == 0) return false;

  String resp;
  int code = uplink_post("/api/v1/ingest",
                         uplink_ingest_json(id, meta, samples, n),
                         "application/json", &resp, 5000);
  if (code != 201) {
    // 失败必须说出失败的样子：状态码 + 服务端给的原因，一起打到串口。
    // 只打一个 "上传失败" 的话，401(令牌错) / 400(request_id 不存在) /
    // 502(服务端没起) 三种完全不同的故障在日志里长得一模一样。
    Serial.printf("[uplink] 上传失败 HTTP %d 样本=%u req=%s resp=%s\n", code, n,
                  meta.request_id ? meta.request_id : "-", resp.c_str());
    return false;
  }

  if (batch_id_out) {
    // 不为了一个整数引 JSON 库：只认 "batch_id": 后面紧跟的数字。
    // 认不出来就填 -1，不影响"这批已被收下"这个已经由 201 确认的事实。
    int k = resp.indexOf("\"batch_id\":");
    if (k >= 0) *batch_id_out = resp.substring(k + 11).toInt();
  }
  return true;
}