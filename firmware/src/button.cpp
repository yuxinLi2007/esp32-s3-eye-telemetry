#include "button.h"

#include <string.h>

#include "config.h"
#include "uplink.h"

// ------------------------------------------------------------------ 常量
// 按下反馈：单闪。刻意最短——它是"板子听到了"的确认，不是表演。
// ack：两下慢闪；cancel：六下快闪。两种图案在眼角余光里也能分辨：
// 佩戴者不会盯着板子看，节奏（慢/快）比次数更容易被感知。
#define PRESS_ON_MS    120
#define ACK_ON_MS      250
#define ACK_OFF_MS     150
#define ACK_TIMES      2
#define CANCEL_ON_MS   60
#define CANCEL_OFF_MS  60
#define CANCEL_TIMES   6

#define BUTTON_HTTP_TIMEOUT_MS 3000UL

// ------------------------------------------------------------------ 状态
static SnapshotFn g_snap = nullptr;

// 消抖：电平稳定 BUTTON_DEBOUNCE_MS 才算数。机械按键一次抖动约 5~20ms，
// 不做消抖的话一次按压会记成好几个 press_seq——服务端幂等键救不了这种重复，
// 因为每一次抖动在板上都是"一个新的序号"。
static int g_raw_last = HIGH;
static int g_stable = HIGH;
static uint32_t g_change_ms = 0;

struct Press {
  uint32_t seq;          // 本次启动内的按键序号（幂等键的一部分）
  uint32_t uptime_ms;    // 按下时刻（板上单调时钟）
  uint8_t tries;
  bool pending;          // true = 还没被服务端确认收下
};
static Press g_queue[BUTTON_QUEUE];
static uint32_t g_press_seq = 0;
// 队列满/重试耗尽而丢弃的按键数。不静默：随下一次成功上传的 queue_dropped 带出去。
static uint32_t g_dropped = 0;
static uint32_t g_last_retry_ms = 0;

// ------------------------------------------------------------------ LED
// 图案引擎是状态机不是 delay：按下反馈发生在 button_poll 里，
// 用 delay 阻塞会把连续流采样一起堵住。
struct LedPattern {
  uint32_t on_ms, off_ms;
  uint8_t left;          // 剩余闪烁次数
  uint32_t t0;
  bool on;
  bool active;
};
static LedPattern g_led = {};

static void led_write(bool on) {
  digitalWrite(PIN_LED, on ? LED_ON_LEVEL : !LED_ON_LEVEL);
}

static void led_play(uint32_t on_ms, uint32_t off_ms, uint8_t times) {
  g_led.on_ms = on_ms;
  g_led.off_ms = off_ms;
  g_led.left = times;
  g_led.t0 = millis();
  g_led.on = true;
  g_led.active = true;
  led_write(true);
}

static void led_tick() {
  if (!g_led.active) return;
  uint32_t now = millis();
  if (g_led.on) {
    if (now - g_led.t0 >= g_led.on_ms) {
      g_led.on = false;
      g_led.t0 = now;
      led_write(false);
      if (--g_led.left == 0) g_led.active = false;
    }
  } else if (now - g_led.t0 >= g_led.off_ms) {
    g_led.on = true;
    g_led.t0 = now;
    led_write(true);
  }
}

static bool led_busy() { return g_led.active; }

// ------------------------------------------------------------------ 上传
// 返回 true 表示服务端已确认收下（201=新事件，200=幂等命中：
// 说明之前某次"失败"的上传其实到了，重试重发正是靠服务端幂等键兜住的）。
static bool upload_press(Press &p) {
  DeviceSnapshot s = {};
  if (g_snap) g_snap(&s);

  // 手写 JSON（板上不引 JSON 库，口径与 uplink.cpp/command.cpp 一致）。
  String j;
  j.reserve(384);
  j += "{\"device_mac\":\"";
  j += s.mac ? s.mac : "";
  j += "\",\"boot_id\":\"";
  j += s.boot_id ? s.boot_id : "";
  j += "\",\"fw_version\":\"";
  j += s.fw_version ? s.fw_version : FW_VERSION;
  j += "\",\"press_seq\":";
  j += String(p.seq);
  j += ",\"t_press_uptime_ms\":";
  j += String(p.uptime_ms);
  j += ",\"ntp_synced\":";
  j += s.ntp_synced ? "true" : "false";
  j += ",\"ntp_sync_age_s\":";
  j += s.ntp_synced ? String(s.ntp_age_s) : String("null");
  j += ",\"t_device_ntp_ms\":";
  if (s.ntp_synced && s.has_device_time) {
    // 按下的 NTP 时刻 = 现在的 NTP 时刻 - (现在的 uptime - 按下时的 uptime)。
    // 只有 NTP 同步过才这么算；没同步就写 null，绝不拿开机时钟冒充墙上时钟。
    char b[24];
    snprintf(b, sizeof(b), "%lld",
             (long long)((int64_t)s.t_device_ntp_ms -
                         (int64_t)(millis() - p.uptime_ms)));
    j += b;
  } else {
    j += "null";
  }
  j += ",\"queue_dropped\":";
  j += String(g_dropped);
  j += "}";

  String resp;
  int code = uplink_post("/api/v1/button", j, "application/json", &resp,
                         BUTTON_HTTP_TIMEOUT_MS);
  if (code == 201 || code == 200) {
    p.pending = false;
    Serial.printf("[btn] 上传成功 seq=%lu HTTP %d%s\n", (unsigned long)p.seq, code,
                  code == 200 ? "（幂等命中：早前那次其实已到达）" : "");
    if (g_dropped) {
      // 这份计数已随本次请求送达服务端，可以销账了
      Serial.printf("[btn] 已随本次上报销账：历史丢弃按键 %lu 次\n",
                    (unsigned long)g_dropped);
      g_dropped = 0;
    }
    return true;
  }
  // 失败说出失败的样子：401(令牌) / 4xx(字段) / 5xx(服务端) / 负值(没网) 各不相同
  Serial.printf("[btn] 上传失败 seq=%lu HTTP %d resp=%s（%lu ms 后重试，"
                "本地反馈已给过，事件不会丢）\n",
                (unsigned long)p.seq, code, resp.c_str(),
                (unsigned long)BUTTON_RETRY_MS);
  return false;
}

// ------------------------------------------------------------------ 按下
static void on_press() {
  // 本地反馈排第一位：先让佩戴者"摸得着"，再谈网络。
  // 这一行执行时，事件甚至还没有序号入队——断网时按钮照样有反应。
  led_play(PRESS_ON_MS, 1, 1);   // 单闪一次，off 段用不到（left 在 on 结束时归零）

  int slot = -1;
  for (int i = 0; i < BUTTON_QUEUE; i++) {
    if (!g_queue[i].pending) { slot = i; break; }
  }
  if (slot < 0) {
    // 队列满 = 上传已经失败了 BUTTON_QUEUE 次以上。丢弃并记账，
    // 账随下一次成功上传报出去——不能既丢事件又不留痕迹。
    g_dropped++;
    Serial.printf("[btn] 待传队列已满（%d 条），本次按键丢弃并记账 "
                  "queue_dropped=%lu\n", BUTTON_QUEUE, (unsigned long)g_dropped);
    return;
  }
  Press &p = g_queue[slot];
  p.seq = g_press_seq++;
  p.uptime_ms = millis();
  p.tries = 1;
  p.pending = true;
  Serial.printf("[btn] 按下 seq=%lu uptime=%lums（LED 已本地反馈）\n",
                (unsigned long)p.seq, (unsigned long)p.uptime_ms);
  upload_press(p);   // 立刻试一次；失败则留在队列里由 button_poll 重试
}

static void scan_button() {
  int raw = digitalRead(PIN_BUTTON);
  uint32_t now = millis();
  if (raw != g_raw_last) {
    g_raw_last = raw;
    g_change_ms = now;
    return;
  }
  if (raw == g_stable) return;
  if (now - g_change_ms < BUTTON_DEBOUNCE_MS) return;
  int prev = g_stable;
  g_stable = raw;
  // 低有效：HIGH->LOW 的下降沿才是"按下"。抬起不产生事件。
  if (prev == HIGH && raw == LOW) on_press();
}

// ------------------------------------------------------------------ 重试
static void retry_pending() {
  bool any = false;
  for (int i = 0; i < BUTTON_QUEUE; i++) if (g_queue[i].pending) { any = true; break; }
  if (!any) { g_last_retry_ms = 0; return; }
  if (g_last_retry_ms != 0 && millis() - g_last_retry_ms < BUTTON_RETRY_MS) return;
  g_last_retry_ms = millis();
  for (int i = 0; i < BUTTON_QUEUE; i++) {
    Press &p = g_queue[i];
    if (!p.pending) continue;
    if (p.tries >= BUTTON_RETRY_MAX) {
      // 放弃这条，但不装作没事：计入 queue_dropped，下一次成功上传时可见。
      p.pending = false;
      g_dropped++;
      Serial.printf("[btn] seq=%lu 重试 %u 次仍失败，放弃并记账 queue_dropped=%lu\n",
                    (unsigned long)p.seq, (unsigned)p.tries,
                    (unsigned long)g_dropped);
      continue;
    }
    p.tries++;
    upload_press(p);
    break;   // 每轮最多传一条：按键重试不该把主循环占住
  }
}

// ------------------------------------------------------------------ 对外
void button_begin(SnapshotFn snap) {
  g_snap = snap;
  pinMode(PIN_BUTTON, INPUT_PULLUP);   // BOOT 键接地触发，内部上拉即可
  pinMode(PIN_LED, OUTPUT);
  led_write(false);
  g_stable = g_raw_last = digitalRead(PIN_BUTTON);
  g_change_ms = millis();
  // 上电单闪：让"LED 到底接没接对"在第一秒就暴露，而不是等到第一次按键。
  led_play(100, 100, 1);
  Serial.printf("[btn] 按键通道就绪 pin=%d(LED=%d) 消抖=%dms 队列=%d\n",
                PIN_BUTTON, PIN_LED, BUTTON_DEBOUNCE_MS, BUTTON_QUEUE);
}

void button_poll() {
  if (!g_snap) return;             // 没接线就什么都不做，不猜
  led_tick();
  scan_button();
  retry_pending();
}

bool button_play_decision(const char *decision, IdleFn idle) {
  uint32_t on, off;
  uint8_t times;
  if (decision && strcmp(decision, "ack") == 0) {
    on = ACK_ON_MS; off = ACK_OFF_MS; times = ACK_TIMES;
  } else if (decision && strcmp(decision, "cancel") == 0) {
    on = CANCEL_ON_MS; off = CANCEL_OFF_MS; times = CANCEL_TIMES;
  } else {
    return false;   // 不认识的 decision：不放任何图案，由调用方如实上报
  }
  Serial.printf("[btn] 播放 %s 反馈（%u 次闪烁）\n", decision, (unsigned)times);
  led_play(on, off, times);
  // 等图案放完。期间回调 idle 让连续流照常采样上传——
  // 与 capture 的等待同一个套路：一次远程操作不该在连续流上挖一个假断层。
  uint32_t t0 = millis();
  while (led_busy() && millis() - t0 < 5000) {
    led_tick();
    if (idle) idle();
    delay(2);
  }
  led_write(false);
  return true;
}

bool button_upload_pending() {
  for (int i = 0; i < BUTTON_QUEUE; i++) if (g_queue[i].pending) return true;
  return false;
}
