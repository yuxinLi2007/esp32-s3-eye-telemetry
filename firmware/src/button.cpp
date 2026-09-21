#include "button.h"

#include <string.h>

#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

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
static uint32_t g_press_seq = 0;

struct Press {
  uint32_t seq;          // 本次启动内的按键序号（幂等键的一部分）
  uint32_t uptime_ms;    // 按下时刻（板上单调时钟）
  uint8_t tries;         // 已尝试上传次数
  bool pending;          // true = 还没被服务端确认收下
};
// 待传队列由按键任务（写入）与主循环（上传/销账）两个任务共同访问，
// 所以下面所有对 g_queue / g_dropped / g_press_seq 的读写都在 g_q_lock 里。
// 不加锁的后果不是"偶尔慢一点"：可能读到半写好的 Press，把一条事件的
// seq 和 uptime 拼成两个不同按键的值，那是一条看起来合法、其实不存在的记录。
static Press g_queue[BUTTON_QUEUE];
static uint32_t g_dropped = 0;      // 队列满/重试耗尽丢弃的按键数，随下次成功上传带出
static uint32_t g_last_retry_ms = 0;
static SemaphoreHandle_t g_q_lock = nullptr;

// ---- 跨任务标志（按键任务 -> 主循环 / 主循环 -> 按键任务）----
// 只传"有没有"这种单字事实，读写本身在目标平台上原子，配合队列锁足够。
static volatile bool g_new_press = false;      // 有新按键，下一轮跳过重试节流立刻发
static volatile bool g_pattern_req = false;    // 主循环请求播放远程反馈
static volatile bool g_pattern_done = false;
static volatile bool g_pattern_running = false;
static volatile uint8_t g_pattern_kind = 0;    // 1=ack 2=cancel

// ------------------------------------------------------------------ LED
// 图案引擎是状态机不是 delay。**LED 只有一个属主：button_task**。
// 主循环想放图案只能登记 g_pattern_req，由 button_task 播放——
// 两个任务同时 digitalWrite 同一个引脚会互相把对方的图案掐掉。
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
// dropped_now 是发这条时随包带出的历史丢弃数，由调用方在持锁时取快照——
// 上传本身可能阻塞好几秒，不能一直占着锁。
static bool upload_press(const Press &p, uint32_t dropped_now) {
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
  j += String(dropped_now);
  j += "}";

  String resp;
  int code = uplink_post("/api/v1/button", j, "application/json", &resp,
                         BUTTON_HTTP_TIMEOUT_MS);
  if (code == 201 || code == 200) {
    Serial.printf("[btn] 上传成功 seq=%lu HTTP %d%s\n", (unsigned long)p.seq, code,
                  code == 200 ? "（幂等命中：早前那次其实已到达）" : "");
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
// 只在 button_task 里执行。第一件事就是闪灯，然后才入队——
// 顺序不能反：任何可能阻塞的事情（比如上传）都必须排在反馈之后。
static void on_press() {
  led_play(PRESS_ON_MS, 1, 1);   // 单闪一次，off 段用不到（left 在 on 结束时归零）

  xSemaphoreTake(g_q_lock, portMAX_DELAY);
  int slot = -1;
  for (int i = 0; i < BUTTON_QUEUE; i++) {
    if (!g_queue[i].pending) { slot = i; break; }
  }
  if (slot < 0) {
    // 队列满 = 上传已经失败了 BUTTON_QUEUE 次以上。丢弃并记账，
    // 账随下一次成功上传报出去——不能既丢事件又不留痕迹。
    g_dropped++;
    uint32_t dropped = g_dropped;
    xSemaphoreGive(g_q_lock);
    Serial.printf("[btn] 待传队列已满（%d 条），本次按键丢弃并记账 "
                  "queue_dropped=%lu\n", BUTTON_QUEUE, (unsigned long)dropped);
    return;
  }
  Press &p = g_queue[slot];
  p.seq = g_press_seq++;
  p.uptime_ms = millis();
  p.tries = 0;
  p.pending = true;
  uint32_t seq = p.seq, up = p.uptime_ms;
  xSemaphoreGive(g_q_lock);

  Serial.printf("[btn] 按下 seq=%lu uptime=%lums（LED 已本地反馈）\n",
                (unsigned long)seq, (unsigned long)up);
  // 上报不在这里做：本任务必须立刻回去推进 LED 图案。
  // g_new_press 让主循环跳过重试节流，下一轮就发。
  g_new_press = true;
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
// 只在主循环执行。HTTP 调用放在锁外面：一次断网上传要等满超时，
// 持锁等它会把按键任务挡在队列外，本地反馈又变成看网络脸色。
static void retry_pending() {
  Press p = {};
  int idx = -1;
  uint32_t dropped_now = 0;

  xSemaphoreTake(g_q_lock, portMAX_DELAY);
  int i = 0;
  for (; i < BUTTON_QUEUE; i++) if (g_queue[i].pending) break;
  if (i == BUTTON_QUEUE) {          // 都传完了
    g_last_retry_ms = 0;
    g_new_press = false;
    xSemaphoreGive(g_q_lock);
    return;
  }
  // 新按键跳过节流：第一次上传不该干等满 BUTTON_RETRY_MS。
  if (g_new_press) { g_new_press = false; g_last_retry_ms = 0; }
  if (g_last_retry_ms != 0 && millis() - g_last_retry_ms < BUTTON_RETRY_MS) {
    xSemaphoreGive(g_q_lock);
    return;
  }
  g_last_retry_ms = millis();

  if (g_queue[i].tries >= BUTTON_RETRY_MAX) {
    // 放弃这条，但不装作没事：计入 queue_dropped，下一次成功上传时可见。
    g_queue[i].pending = false;
    g_dropped++;
    uint32_t dropped = g_dropped, seq = g_queue[i].seq;
    uint8_t tries = g_queue[i].tries;
    xSemaphoreGive(g_q_lock);
    Serial.printf("[btn] seq=%lu 重试 %u 次仍失败，放弃并记账 queue_dropped=%lu\n",
                  (unsigned long)seq, (unsigned)tries, (unsigned long)dropped);
    return;
  }
  g_queue[i].tries++;
  p = g_queue[i];
  idx = i;
  dropped_now = g_dropped;
  xSemaphoreGive(g_q_lock);

  // 不持锁做 HTTP。
  bool ok = upload_press(p, dropped_now);
  if (!ok) return;                  // 还留在队列里，等下一轮

  xSemaphoreTake(g_q_lock, portMAX_DELAY);
  // 只销自己那一条：期间可能又有新按下占了别的槽位。
  if (g_queue[idx].pending && g_queue[idx].seq == p.seq) g_queue[idx].pending = false;
  if (dropped_now) {
    // 这份计数已随本次请求送达服务端，可以销账了
    g_dropped -= dropped_now;
    Serial.printf("[btn] 已随本次上报销账：历史丢弃按键 %lu 次\n",
                  (unsigned long)dropped_now);
  }
  // 立刻排空剩余积压：网络刚恢复时不该每条之间还干等 BUTTON_RETRY_MS。
  // g_last_retry_ms 归零 = 下一轮 button_poll 马上再发一条，直到队列清空。
  // 只在"确实还有待传"时才清，避免空转时每个循环都发请求。
  bool more = false;
  for (int k = 0; k < BUTTON_QUEUE; k++) if (g_queue[k].pending) { more = true; break; }
  if (more) g_last_retry_ms = 0;
  xSemaphoreGive(g_q_lock);
}

// ------------------------------------------------------------------ 对外
// 按键扫描与 LED 必须独立于网络。
//
// 真机实测（2026-09-22）：服务端停下后，主循环里的 HTTP 调用要等满超时，
// 上传节拍从 2s 变成 8.02s（每轮被堵约 6 秒）。按键原本也在主循环里轮询，
// 一次普通点按（约 100ms）整个落进这个阻塞窗口，扫描根本看不见——
// "断网按键不闪灯"不是操作问题，是本地反馈被网络超时卡死了。
// 修法：按键扫描 + LED 图案放进独立任务，主循环只负责重试上传。
// 这样只要板子还有电，"按下 -> 立刻闪"就成立，与网络状态彻底无关。
static void button_task(void *) {
  for (;;) {
    led_tick();

    // 主循环登记的远程反馈（回应/取消）在这里播放——LED 只有一个属主。
    if (g_pattern_req && !g_pattern_running) {
      uint8_t kind = g_pattern_kind;
      g_pattern_req = false;
      g_pattern_running = true;
      if (kind == 1) led_play(ACK_ON_MS, ACK_OFF_MS, ACK_TIMES);
      else if (kind == 2) led_play(CANCEL_ON_MS, CANCEL_OFF_MS, CANCEL_TIMES);
    }
    if (g_pattern_running && !led_busy()) {
      g_pattern_running = false;
      g_pattern_done = true;
    }

    scan_button();
    vTaskDelay(pdMS_TO_TICKS(5));   // 5ms 轮询：消抖后仍能捕获普通点按
  }
}

void button_begin(SnapshotFn snap) {
  g_snap = snap;
  if (!g_q_lock) g_q_lock = xSemaphoreCreateMutex();
  pinMode(PIN_BUTTON, INPUT_PULLUP);   // BOOT 键接地触发，内部上拉即可
  pinMode(PIN_LED, OUTPUT);
  led_write(false);
  g_stable = g_raw_last = digitalRead(PIN_BUTTON);
  g_change_ms = millis();
  // 上电单闪：让"LED 到底接没接对"在第一秒就暴露，而不是等到第一次按键。
  led_play(100, 100, 1);
  Serial.printf("[btn] 按键通道就绪 pin=%d(LED=%d) 消抖=%dms 队列=%d\n",
                PIN_BUTTON, PIN_LED, BUTTON_DEBOUNCE_MS, BUTTON_QUEUE);
  // 优先级 3 > loop 的 1：主循环正卡在 HTTP 超时里时，本任务照样按时扫描。
  xTaskCreatePinnedToCore(button_task, "button", 4096, nullptr, 3, nullptr, 1);
  Serial.println("[btn] 按键任务已启动：本地闪灯与网络阻塞隔离，断网也照闪");
}

void button_poll() {
  if (!g_snap) return;             // 没接线就什么都不做，不猜
  // 扫描与 LED 已交给 button_task；这里只做上行重试（会被网络超时阻塞）。
  retry_pending();
}

bool button_play_decision(const char *decision, IdleFn idle) {
  uint8_t kind;
  if (decision && strcmp(decision, "ack") == 0) {
    kind = 1;
  } else if (decision && strcmp(decision, "cancel") == 0) {
    kind = 2;
  } else {
    return false;   // 不认识的 decision：不放任何图案，由调用方如实上报
  }
  Serial.printf("[btn] 播放 %s 反馈\n", decision);
  // 图案由 button_task 播放，这里只登记请求并等它放完。等的时候回调 idle，
  // 让连续流照常采样上传——与 capture 的等待同一个套路，不挖假断层。
  g_pattern_done = false;
  g_pattern_kind = kind;
  g_pattern_req = true;
  uint32_t t0 = millis();
  while (!g_pattern_done && millis() - t0 < 5000) {
    if (idle) idle();
    delay(2);
  }
  return true;
}

bool button_upload_pending() {
  bool any = false;
  if (!g_q_lock) return false;
  xSemaphoreTake(g_q_lock, portMAX_DELAY);
  for (int i = 0; i < BUTTON_QUEUE; i++) if (g_queue[i].pending) { any = true; break; }
  xSemaphoreGive(g_q_lock);
  return any;
}
