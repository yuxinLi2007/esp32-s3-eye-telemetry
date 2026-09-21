# 第3周：按键触发与物理反馈闭环

前两周把"设备 → 云端 → 网页"和"网页 → 指令 → 设备"两条单向链路各自跑通了。
第3周把它们接成一个**闭环**，并且把"人"放进环里：

> 佩戴者按下板上按键 → **本地立刻**给物理反馈（不等网络）→ 事件上报 VPS →
> Web 端 2 秒内出现这一行 → 操作者点「回应」或「取消」→ 复用第2周的指令通道
> 下发一条 `notify` → 设备播放对应的 LED 图案 → 指令状态回到 Web 同一行。

三条口径不变，只是这一周多了一条最硬的：

- **失败必须可见**：断网时按的键、板上丢弃的键、设备没来领的 notify、
  固件太旧不认识的 decision，都要在界面/串口上有各自的样子。
- **单一事实来源**：事件状态机只在 `server/buttons.py`，指令状态机只在
  `server/commands.py`；事件行**不存指令状态的副本**，要看指令死活就现查。
- **派生值不落库**：`age_ms`、`state_text`、`command.*` 都是算出来的。
- **新增：本地反馈不许依赖网络**。按键的物理反馈必须在 Wi-Fi 关联之前就能发生，
  否则"按了没反应"和"板子死了"在佩戴者眼里是同一件事。

---

## 第一步：第2周结构分析 —— 哪些模块必须动

| 文件 | 第2周职责 | 第3周改动 | 为什么这么改 |
| --- | --- | --- | --- |
| `firmware/src/main.cpp` | setup + loop（采集 / 上报 / `command_poll`） | **只加 2 处接线**：`button_begin(fill_snapshot)`；loop 里 `button_poll()` 放在 **Wi-Fi 检查之前** | 要求是"功能逻辑不写在 main"。放在 Wi-Fi 检查之前不是风格问题：没网也必须消抖+闪灯，否则断网时按键完全没反应 |
| `firmware/src/button.h/.cpp` | — | **新增**：消抖、LED 图案状态机、待传队列、上报与重试、`button_play_decision()` | 第3周板端逻辑全部在这里，`main.cpp` 不碰按键细节 |
| `firmware/src/command.cpp` | ping / selftest / capture | `Command` 结构体 +`decision[8]` / `event_id`；`parse_claim` 认这两个键；新增 `run_notify()`；调度表 +1 行 | notify 走的还是第2周那条领取/回执链路，**一行通信代码都没重写** |
| `firmware/include/config.h` | 采样/上传/指令轮询参数 | +`PIN_BUTTON 0`、`BUTTON_DEBOUNCE_MS 30`、`BUTTON_QUEUE 8`、`BUTTON_RETRY_MS 5000`、`BUTTON_RETRY_MAX 10`、`PIN_LED 21`、`LED_ON_LEVEL HIGH`、`PIN_LED_VERIFIED 0`；`FW_VERSION` → `0.3.0` | 消抖窗口、队列深度、重试节流都必须是显式常量：它们是"最坏丢几次按键"的直接来源 |
| `server/buttons.py` | — | **新增**：`button_events` 表、事件状态机、`record_press` / `respond` / `list_events` / `stats` | 事件与指令是两种东西（一个是"人做了什么"，一个是"云端让设备做什么"），分表；但**指令部分完全委托 `commands.create()`** |
| `server/commands.py` | 4 个 op + 参数校验 + 状态机 | OPS +`notify`；参数类型 +`STR`（带 enum 白名单）；`ops_catalog` 给出 `choices` | 之前所有参数都是整数，`decision=ack/cancel` 逼出了字符串类型；enum 白名单是防注入的第一道，`_coerce` 是第二道 |
| `server/app.py` | ingest / readings / status / control / commands | +3 个端点（上报 / 列表 / 回应）；lifespan 里 `buttons.init_schema(conn)` | 鉴权口径完全沿用：上报用 `INGEST_TOKEN`（板子上已配），Web 回应用 `CONTROL_TOKEN`（能改设备行为的动作），事件列表公开只读 |
| `server/static/index.html` | 图表 + 指标卡 + 指令面板 | +「按键事件（第3周）」区块（7 列表格）；`<script src="button.js">` | 复用已有 badge CSS（`b-pending`/`b-done`/`b-cancelled`），不新造一套视觉语言 |
| `server/static/button.js` | — | **新增**：事件面板全部逻辑 | |
| `server/static/commands.js` | 指令面板 | `renderOps` 泛化：所有带参数的 op 都渲染输入（str→`<select>`，int→number）；`resultSummary` 认 `decision`/`event_id`/`led_pin_note` | 原来写死了"参数就是数字"；notify 一来就必须泛化，否则前端要为新 op 改代码——那就违背了"ops 目录是唯一来源" |
| `server/tools/command_sim.py` | 假设备 + 故障注入 | 新增 **S11 按键闭环**（26 条断言）；S0 白名单断言跟着 OPS 走 | |
| `server/tools/ui_check.mjs` | 前端回归 | 新增按键面板段：渲染断言 + **回应防抖/幂等键断言** + notify 的 `decision` 枚举断言 | |
| `server/test_buttons.py` | — | **新增**：25 个单测（含 6.5 状态防伪 2 个） | |

**没有改的**：传感器读取路径、`uplink.*`（HTTP 出口）、图表绘制、
时间戳可信度口径、`request_id` 生成规则、指令状态机本体、入库鉴权。

---

## 第二步：闭环时序与两套状态

### 2.1 全链路

```
 佩戴者                板子(0.3.0)                 VPS                     Web
   │                      │                         │                       │
   │──按下 GPIO0─────────▶│                         │                       │
   │                      │ 消抖 30ms，HIGH→LOW 沿   │                       │
   │◀─LED 单闪 120ms──────│ ← 本地反馈，此时还没网也不影响                    │
   │                      │ 入队 seq++，立刻 POST /api/v1/button              │
   │                      │────────────────────────▶│ 幂等键(mac,boot,seq)   │
   │                      │◀───201 新建 / 200 命中───│ 写 button_events       │
   │                      │                         │◀────每 2s 轮询────────│
   │                      │                         │  GET /button/events    │
   │                      │                         │──────────────────────▶│ 行出现「待回应」
   │                      │                         │                       │
   │                      │                         │◀──点「回应」──────────│ POST .../respond
   │                      │                         │ buttons.respond()     │  (client_token)
   │                      │                         │  └▶ commands.create() │
   │                      │                         │      op=notify        │ 201 / 200+X-Deduped
   │                      │◀──每 2s claim───────────│ 事件行 ← request_id   │
   │                      │ rid|notify|decision=ack;event_id=7|15000        │
   │◀─LED 两下慢闪────────│ run_notify()            │                       │
   │                      │──POST /commands/{rid}/done {led_feedback:true}─▶│
   │                      │                         │ 指令 → done           │ 同一行显示「成功」
   │                      │                         │──────────────────────▶│ 「设备已播放反馈」
```

关键顺序：**LED 先闪，再回执**。回执说的必须是已经发生的事，
不能"先说成功再去做"——那样一次掉电就会在界面上留下一条假的成功。

### 2.2 事件状态只有三态

```
        POST /api/v1/button
                 │
                 ▼
          ┌────────────┐  respond(decision=ack)    ┌─────────┐
          │  received  │──────────────────────────▶│  acked  │
          │   待回应    │                            └─────────┘
          └────────────┘                                   │
                 │                                         │ 可以再次 respond
                 │  respond(decision=cancel)  ┌─────────────┘ （例如重发/改主意）
                 └──────────────────────────▶│
                                        ┌───────────┐
                                        │ cancelled │
                                        └───────────┘
```

**刻意没有"设备已确认"这个状态。** 设备执行 notify 的结果
（`done` / `failed` / `timeout` / `expired`）已经完整记录在 `commands` 表里，
再抄一份到 `button_events` 就是第二个事实来源，两边迟早对不上。
界面要显示"设备真的收到回应了吗"，就按 `request_id` 现查那条指令：

| 界面显示 | 来源 |
| --- | --- |
| 事件徽标「待回应/已回应/已取消」 | `button_events.state` |
| notify 徽标「等待设备领取/成功/超期未领/执行超时/设备报错/已撤销」 | `commands.state`（每次查询现算，含 `sweep()`） |
| 「重发」按钮 | 事件有 `request_id` **且** 指令已终态 **且** 状态 ≠ done |

`respond_count` 记的是"用户点过几次回应"。每次回应都产生一条**新**指令，
事件行只保留最近一次的 `request_id`；历史在 `commands` 表里
（`params_json` 带 `event_id`，可反查），不重复存。

### 2.3 幂等键：每一层各一把，各管一件事

| 层 | 键 | 防的是 |
| --- | --- | --- |
| 板上消抖 | 电平稳定 `BUTTON_DEBOUNCE_MS=30` 才算一次 | 机械抖动把**一次按压**记成好几个 `press_seq`——这种重复服务端**救不了**，因为在板上它们真的是不同序号 |
| 上报 | `UNIQUE(device_mac, boot_id, press_seq)` | HTTP 超时重发（其实服务端已收下）变成"用户按了两次"。命中返回 **200 + `X-Deduped:1`**，新建返回 201，状态码本身就说明这是不是第一次 |
| Web 回应 | `client_token`（前端每次点击生成 `web-<ts>-<rand>`，同一次点击的所有重试复用） | 双击 / 断网点两次 → 设备闪两遍灯 |
| Web 回应（无 token 时） | 服务端派生 `btn<id>-<decision>-r<respond_count+1>` | 同上，并且给了"网络重发不重复、用户重点即重发"的正确语义：计数没变 → 命中同一条指令；用户 intentional 再点一次 → 计数已加 → 新指令 |
| 指令 | `request_id`（第2周那套，`req_<base36 时间>_<随机>`） | 板端重领、回执重放 |

`boot_id` 让 `press_seq` 可以从 0 重来：重启后序号归零不会撞上重启前的记录，
因为 `boot_id` 变了。这一条在 `command_sim.py` S11 里专门有断言
（同一 mac、同一 seq=0、不同 boot_id → 必须是两条事件）。

---

## 第三步：后端

### 3.1 端点一览

| 方法与路径 | 鉴权 | 用途 | 成功码 |
| --- | --- | --- | --- |
| `POST /api/v1/button` | `INGEST_TOKEN` | 板端上报一次按键 | **201** 新建 / **200** 幂等命中（`X-Deduped:1`） |
| `GET /api/v1/button/events` | 无（只读） | Web 轮询：`limit`(≤200)、`device_mac`、`state`（逗号分隔） | 200，含 `stats` + `events[]`（每条带 `command` 摘要） |
| `POST /api/v1/button/events/{id}/respond` | `CONTROL_TOKEN` | Web 回应：`{decision: ack\|cancel, client_token?}` | **201** 新指令 / **200** 幂等命中 |

错误码沿用第2周口径（`bad_mac` / `bad_boot_id` / `bad_seq` / `bad_decision` /
`not_found` / `db_error` / `too_many_inflight` / `unsupported_op` …），
4xx 带 `error.code` + `error.message`，前端原样显示，不吞。

`GET /events` 里第一件事是 `commands.sweep(conn, now)`：
惰性结算指令超时（与 `/api/v1/status` 同一机制，不起后台线程）。
于是"这条 notify 还活着吗"在按键面板和指令面板看到的永远是同一个答案。

### 3.2 `button_events` 表

| 列 | 含义 | 备注 |
| --- | --- | --- |
| `device_mac` / `boot_id` / `press_seq` | 幂等键 | `UNIQUE` 索引 `uq_button_press` |
| `fw_version` | 板端固件版本 | `notify` 是 0.3.0 才有的 op，旧固件会回 `unsupported_op`，这一列让"为什么失败"可对号 |
| `t_press_uptime_ms` | 按下时刻（板上单调时钟） | 只作参考 |
| `t_device_ntp_ms` | 按下时刻（板端 NTP 换算） | 可为 `NULL`。**没同步过 NTP 就写 NULL，绝不拿开机时钟冒充墙上时钟** |
| `ntp_synced` / `ntp_sync_age_s` | 可信度依据 | 前端按第1周同一套 `trustOf()` 规则打标 |
| `queue_dropped` | 板端累计丢弃的按键数 | 见 4.4 |
| `t_server_ms` | **权威时间** | 所有排序、age 都用它 |
| `state` / `decision` / `request_id` / `t_decided_ms` / `decided_by` / `respond_count` | 回应记录 | `decision` 只是"用户点了什么"，不是"设备做了什么" |

板端按下时刻的换算（`button.cpp`）：
`t_device_ntp_ms = 现在的 NTP 时刻 − (现在的 uptime − 按下时的 uptime)`。
用 uptime 差值而不是"上传时的 NTP 时刻"，否则重试几分钟后事件会被记到未来。

迁移策略与第2周一致：**只加不改不删**（`CREATE TABLE IF NOT EXISTS` + `migrate()`
补列）。`migrate()` 现在是空的，但函数先立在那里——"升级不删库"要从第一列开始就成立，
真实库 `server/data/telemetry.db` 已经有 24MB 数据，不能拿它做实验。

### 3.3 `notify` op（`commands.py` 里 +1 条，不新造通道）

```python
"notify": {
    "params": {
        "decision": {"kind": STR, "choices": ("ack", "cancel"), "default": "ack"},
        "event_id": {"kind": INT, "lo": 0, "hi": 1 << 40, "default": 0},
    },
    "ttl_ms": 30_000,      # 30s 没人领 → expired，界面上给「重发」
    "timeout_ms": 15_000,  # 领了没回执 → timeout
}
```

这是 `commands.py` 里第一个字符串参数，所以顺带补了 `STR` 类型：
`_coerce` 做 enum 白名单校验（不在 `choices` 里直接 `bad_param`），
`ops_catalog` 把 `choices` 一并吐给前端——前端于是能渲染出下拉框，
而不是硬编码 `ack`/`cancel` 两个字符串。**参数白名单在服务端只有一份，
前端从 `/commands/ops` 读**，这条第2周立的规矩在第一个字符串参数上就守住了。

`ttl_ms` 只有 30 秒（比 capture 短得多）：notify 是"即时反馈"，
一条 5 分钟后才被领取的"请闪两下灯"没有意义，不如早早 expired 让人看见并重发。

### 3.4 `respond()` 只做两件事

1. `commands.create(op="notify", params={decision, event_id}, client_token=…)`
   —— 幂等、在飞上限、状态机、审计事件全部复用第2周那一份，一行都不重写；
2. 指令**不是**幂等命中时，才更新事件行（`state`/`decision`/`request_id`/
   `t_decided_ms`/`decided_by`/`respond_count+1`）。

顺序很重要：先创建指令再更新事件。如果反过来，指令创建失败（例如在飞已满
`too_many_inflight`）就会留下一条"已回应但没有指令"的事件——那种状态无法自愈。
现在失败的话事件行原封不动，界面提示错误，用户重试即可。

---

## 第四步：设备端

### 4.1 本地反馈优先，且离线可用

```cpp
void loop() {
  button_poll();      // ← 在 Wi-Fi 检查之前：没网也要消抖 + 闪灯 + 入队
  if (WiFi.status() != WL_CONNECTED) { … return; }
  command_poll();
  …
}
```

`on_press()` 的第一行就是 `led_play(PRESS_ON_MS, 1, 1)`——
执行到这一行时事件甚至还没拿到序号。断网、DNS 挂了、VPS  down 了，
按键的"咔哒"反馈都在。

### 4.2 消抖为什么必须在板上做

机械按键一次抖动静测 5~20ms。若在板上不消抖，一次按压会产生好几个
`press_seq`，服务端的幂等键**救不了**——因为在服务端看来那确实是"不同的按键"。
所以：电平稳定 `BUTTON_DEBOUNCE_MS=30ms` 才算数，且只认 **HIGH→LOW 下降沿**
（抬起不产生事件）。`INPUT_PULLUP` + BOOT 键接地，无需外部电阻。

### 4.3 LED 图案是状态机，不是 `delay`

| 场景 | 图案 | 参数 |
| --- | --- | --- |
| 上电 | 单闪 | 100/100 ×1（让"LED 接没接对"在第一秒暴露，而不是等第一次按键） |
| 按下（本地） | 单闪 | on 120ms |
| `decision=ack` | 两下**慢**闪 | on 250 / off 150 ×2 |
| `decision=cancel` | 六下**快**闪 | on 60 / off 60 ×6 |

用 `delay()` 阻塞会把连续流采样一起堵住——那会在图表上挖出一个假断层，
看起来像设备掉线。所以 `led_tick()` 每次 `button_poll()` 推进一步；
`button_play_decision()` 等图案放完的循环里回调 `idle()`（= `stream_tick`），
与第2周 capture 的等待同一个套路：**一次远程操作不该在连续流上留下假的缺口**。

慢/快两种节奏而不是"闪 2 下 vs 闪 6 下"：佩戴者不会盯着板子看，
节奏比次数在眼角余光里更容易分辨。

### 4.4 队列与 `queue_dropped`：丢事件也要留痕

`BUTTON_QUEUE=8` 条待传槽位。上传失败留在队列里，`button_poll()` 每
`BUTTON_RETRY_MS=5000ms` 重试一条（每轮只传一条，按键重试不许占住主循环），
单条最多 `BUTTON_RETRY_MAX=10` 次。两种情况会丢：

1. 队列满（说明已经连着失败 8 次以上）；
2. 单条重试用尽。

**都不静默**：`g_dropped++`，串口打一行，并随**下一次成功上传**的
`queue_dropped` 字段带到服务端；服务端入库后板端销账。界面上那一行会显示
「板上曾丢弃 N 次」。于是"断网期间按了 12 次只上来 9 次"这件事是有证据的，
而不是变成"网页少了几行，谁知道呢"。

上报接受 **200 或 201**：200 意味着幂等命中——之前那次"失败"的上传其实到了
（超时但服务端已写库）。这正是幂等键存在的意义，板端把它当成功处理并打印说明。

### 4.5 ⚠ `PIN_LED=21` / `LED_ON_LEVEL=HIGH` **尚未在硬件上验证**

这是本周唯一一处**靠猜**的地方，必须在板子接上后第一件事就核对：

- ESP32-S3-EYE 的板载 LED 引脚号在不同批次/版本文档里写法不一（21 / 48 / 无板载 LED），
  本次按常见资料取 `GPIO21`，**没有实物确认**。
- 有效电平同样未确认：若是低有效，现象是"LED 常亮、闪的时候反而灭"。
- 因此 `config.h` 里立了一个开关：

```c
#define PIN_LED_VERIFIED   0   // 硬件核对通过后改成 1
```

`PIN_LED_VERIFIED=0` 时，notify 回执里带 `led_pin_note="pin_unverified"`，
一路显示到 Web 指令面板的结果摘要里。也就是说：**在引脚核对之前，
界面上每一次"成功"都自带一句"灯可能没真亮"的免责说明**，
不会出现"服务端说成功、佩戴者其实什么也没看到"却没人知道的情况。

如果 GPIO21 上没有 LED，改用外接 LED（串 220Ω～1kΩ 电阻到 GND）
或把 `PIN_LED` 改成实测引脚，然后：改 `PIN_LED_VERIFIED=1`、
`FW_VERSION` → `0.3.1`、重新烧写。

### 4.6 回执

```json
{"decision":"ack","event_id":7,"led_feedback":true,"led_pin_note":"pin_unverified"}
```

`decision` 非法时不放任何图案，直接 `post_failed(..., "bad_param", ...)`。
固件不认识 `notify` 这个 op（旧固件）时走第2周已有的 `unsupported_op` 分支，
界面上是一条明确的失败，而不是"执行中"一直转到超时。

---

## 第五步：前端

`server/static/button.js`（独立 IIFE，靠 `window.DASH` 复用第2周暴露的
`$`/`api`/`el`/`fmtTime`/`fmtAge`/`trustOf`/`ctlToken`）：

- **2 秒轮询** `/api/v1/button/events?limit=30`，与指令面板同节奏
  （"实时显示"的口径全站一致）。状态不自己记：另一个标签页回应了，这里 2 秒内就能看到。
- 7 列：服务端收到时刻(+age) / 事件(#id, seq) / 设备(mac@boot) /
  板端按下时刻(+可信度徽标) / 事件状态(+丢弃记账) / notify 指令(request_id + 状态 + error_code) / 操作。
- 操作列三种样子：
  - `received` → 「回应」「取消」两个按钮；
  - 指令已终态但 ≠ done（expired/timeout/failed/cancelled）→ 「重发：回应/取消」；
  - 指令 done → 「设备已播放反馈」；在飞 → 「指令在飞，等结果…」。
- **两层防抖**：`if (busyId !== null) return;`（代码层）+ `disabled`（DOM 层）；
  同一次点击的所有网络重试共用一个 `client_token`。
- 401 走与 `commands.js` 同一套流程、同一个存储键：`CONTROL_TOKEN` 只需要输一次。
- 没有事件时渲染一行 `colSpan=7` 的空态提示（写清"按 GPIO0，LED 立刻单闪，
  最坏 2 秒后出现在这里"），而不是一片空白——空白和"面板挂了"看起来一模一样。

`commands.js` 的泛化：`renderOps` 不再假设"参数就是数字"，
`str` 参数渲染成 `<select>`（选项来自 `ops_catalog` 的 `choices`），
`int` 渲染成带 `min`/`max` 的 number；`resultSummary` 认
`decision`/`event_id`/`led_pin_note`。**新增一个 op 依然不需要改前端代码。**

---

## 第六步：鲁棒性测试

### 6.1 单元/契约层 —— `pytest`（130 passed）

`server/test_buttons.py` 25 个，重点：

- 幂等：同键重发 → `deduped=True`、**不新增行**、原字段不被覆盖；
- 不同 `boot_id` + 同 `press_seq` → 必须是两条；
- 字段校验：`bad_mac` / `bad_boot_id` / `bad_seq`（含 `True` 这种"看着像 1 的布尔"）；
- `respond`：新指令 201 语义、`client_token` 幂等、派生 token 的
  "重点即重发"语义、`bad_decision`、`not_found`、事件行只在非 deduped 时更新；
- `list_events`：`state` 过滤（含非法 state → `bad_state`）、指令摘要现查、
  `sweep()` 被调用（expired 的 notify 在事件列表里也必须是 expired）；
- `ops_catalog` 是前端唯一来源（`test_commands.py` 的 op 集合断言已含 `notify`）；
- **状态防伪**：上报载荷里塞 `state/decision/request_id/id` 一律被丢掉（见 6.5）。

> 踩到的坑：fixture 里必须**同时** monkeypatch `commands.now_ms` 和
> `buttons.now_ms`——`from commands import now_ms` 是绑定副本，
> 只改一边会让"超时判定"用真实时钟，测试变成时序彩票。

### 6.2 端到端层 —— `tools/command_sim.py` S11 + S12（188 passed / 0 failed）

S11 打真实 HTTP，26 条断言，覆盖：新建 201 / 重发 200+`X-Deduped` /
换 `boot_id` 后 seq=0 再来一条 / 列表与 stats / respond 201 / respond 幂等 200 /
`bad_decision` 400 / **claim 文本协议**（`rid|notify|decision=ack;event_id=1|15000`，
参数按字母序，`decision` 在 `event_id` 前）/ done 回执后事件行回显 /
`respond_count` 递增 / 再次 respond 产生新指令 / cancel 路径 / `queue_dropped` 透传。

S12（第3周补的**状态防伪**段，19 条）见 6.5。

### 6.3 前端回归层 —— `tools/ui_check.mjs`

新增两段断言（对着真实服务端数据跑）：

- `notify` 的 `decision` 枚举框必须是 `ack/cancel`（前端硬编码就挂）；
- 按键面板：至少渲染一行（空态也算一行，空白算失败）；打印统计栏/各行徽标；
- **回应防抖**：把 `confirm` 临时放开（只点「回应」，不点「取消」），
  连点 3 次 → 必须只有 1 次 `POST /respond`，且 `client_token` 存在且唯一；
  完成后按钮必须恢复可用（"下一次点击是新意图"）。

### 6.4 固件

`pio run` 通过：RAM 17.7% / Flash 27.7%。**未上板**——见 4.5 与下面的清单。

### 6.5 状态防伪 —— 设备的"我"不能替服务端的"事实"作证

闭环里有两套状态（事件三态、指令现查），它们**只能由服务端写**。
设备侧任何字段都是"设备自称"，不是事实。这一段的断言就是把这条边界钉死：

| 被伪造的东西 | 谁会受害 | 服务端实际行为 | 断言 |
| --- | --- | --- | --- |
| 上报载荷里带 `state:"acked"` | 没人点过回应，界面却显示"已回应" | 载荷字段被忽略，状态强制 `received` | 6.5 第 1 组 |
| 带 `decision` / `request_id` / `respond_count` | 事件行凭空挂上一条不存在的指令 | 一律不写库 | 6.5 第 1 组 |
| 带 `id` / `t_server_ms` | 覆盖别人的行、倒填权威时间 | 主键与权威时间都由服务端生成 | 6.5 第 1 组 |
| 无令牌直接 POST | 任意人伪造按键/回应 | `401`（`X-Ingest-Token` / `X-Control-Token`） | 6.5 第 2 组 |
| `decision` 里带 `;` `=` | 用分隔符往 claim 参数段里注入 | enum 白名单拒掉 `bad_decision` | 6.5 第 3 组 |
| 设备自报"这条指令 done 了" | 服务端显示成功，其实没人领过 | 未领取 → `not_claimed 409` | 6.5 第 4 组 |
| 终态指令再补一条回执 | 已经显示过的"成功/超时"被改写 | 终态不可改 → `already_terminal 409` | 6.5 第 4 组 |

自动化跑法（一条命令覆盖上表）：

```powershell
cd server
$env:INGEST_TOKEN="..."; $env:CONTROL_TOKEN="..."
python -X utf8 tools\command_sim.py --url http://127.0.0.1:8001 --fast   # 看 [S12] 段
python -X utf8 -m pytest -q test_buttons.py -k "forged or separator"      # 2 个单测
```

> **前置条件**：`401` 那两条只在实例真设了令牌时才断言。没设令牌的实例对任何人都开放，
> 那时"无令牌被拒"不成立，脚本会打印 `SKIP` 而不是假通过。

手工验证"状态不能伪造"（不需要板子，照着粘贴即可）：

```powershell
# 伪造一条"已回应"的上报，看服务端给回的真实状态
$H = @{ "X-Ingest-Token"="<INGEST_TOKEN>"; "Content-Type"="application/json" }
$body = '{"device_mac":"94:A9:B8:10:00:0F","boot_id":"FORGE1","press_seq":1,"state":"acked","decision":"ack","request_id":"req_forged","t_server_ms":1}'
Invoke-WebRequest -Uri http://127.0.0.1:8000/api/v1/button -Method POST -Headers $H -Body $body | Select-Object -ExpandProperty Content
# 期望：返回体里 "state":"received"、"decision":null、"request_id":null、t_server_ms 是当下时间
# 且网页该行显示「待回应」——伪造的 acked 没有生效
```

再看页面：刷新网页，这一行必须是**待回应**，操作列有「回应」「取消」。
如果它显示"已回应"，说明服务端采信了客户端状态——第3周的闭环语义就塌了。

### 6.6 断网本地触发 —— 怎么测"本地反馈不依赖网络"

这是第3周最硬的一条：**按下 → 立刻闪灯**必须发生在 Wi-Fi 关联之前。
代码上由 `main.cpp` 的结构保证（`button_poll()` 在 `WiFi.status()` 判断**之外**），
但要证明它成立，必须在断网条件下真按一次。

**板端（真机，主验证）：**

1. 烧好 0.3.0，串口监视器打开，确认能上网、网页有数据（先证明基线是通的）。
2. 制造断网。三选一，**推荐第 1 种**（最接近"佩戴时走出覆盖范围"）：
   - 关掉手机热点 / 关掉路由器 —— 板子会不停地重连，符合真实断网；
   - 把路由器加一条 MAC 黑名单，只断开这块板子（不影响你上网）；
   - 改 `secrets.h` 里 SSID 为不存在的名字重烧（最彻底，但每次要重烧，不推荐）。
3. 断网后**立刻连按 3 次 BOOT 键**。期望：
   - 每按一次，LED **立刻**单闪一次——不卡顿、不等待、不滞后；
   - 串口出现 `[btn] 按下 seq=0/1/2 …（LED 已本地反馈）`；
   - 串口出现 `[btn] 上传失败 seq=… HTTP -1（…5000 ms 后重试，本地反馈已给过，事件不会丢）`；
   - **这一步就是结论**：本地反馈和上报是两条路，网络断了不影响反馈。
4. 恢复网络，等最多 `BUTTON_RETRY_MS`（5 秒）。期望：
   - 串口 `[btn] 上传成功 seq=… HTTP 201`，**3 条都会补上**；
   - 网页出现 3 行「待回应」，时间戳按服务端收到时刻（不是按下时刻）。
5. 验证"丢事件也要留痕"：断网期间连按 **12 次**（超过 `BUTTON_QUEUE=8`）。期望：
   - 前 8 次进队列，第 9 次起串口打印
     `[btn] 待传队列已满（8 条），本次按键丢弃并记账 queue_dropped=N`；
   - 恢复网络后，上来的事件里带上 `queue_dropped`，网页该行显示
     「板上曾丢弃 N 次」。**丢的按键数不许静默消失。**

**服务端（不需要板子，验证"断网期间的响应会被判失败"）：**

设备离线时在网页点「回应」，`notify` 的 ttl 是 30 秒，没人来领 → 到点判 `expired`：

```powershell
cd server
$env:TELEMETRY_DB=".\data\sim-offline.db"; $env:INGEST_TOKEN="..."; $env:CONTROL_TOKEN="..."
python -X utf8 -m uvicorn app:app --port 8001 --env-file .env   # 另开终端
# 造一条按键事件，再回应它，但**不**跑假设备去领取：
python -X utf8 tools\command_sim.py --url http://127.0.0.1:8001     # 不加 --fast，等 30 秒
```

期望：网页该行先显示"已回应 / 等待设备领取"，30 秒后指令变 **expired**，
行上出现「重发」入口——而不是一直转圈假装还在等。
`command_sim.py` 的 S10「设备离线」段正是这条的自动化版本。

---

---

## 怎么跑

```powershell
# 1) 服务端（用独立库，别污染真实数据）
cd server
$env:TELEMETRY_DB="D:\esp32-s3-eye-telemetry\.scratch\sim3.db"
$env:INGEST_TOKEN="<与 secrets.h 一致>"
python -X utf8 -m uvicorn app:app --port 8001 --env-file .env

# 2) 端到端（另开一个终端）
cd server
python -X utf8 tools\command_sim.py --url http://127.0.0.1:8001 --fast

# 3) 前端回归（同一个 8001 实例）
cd server
node tools\ui_check.mjs --origin http://127.0.0.1:8001

# 4) 单元测试
cd server
python -X utf8 -m pytest -q          # 130 passed

# 5) 固件
cd firmware
pio run                              # 编译；烧写：pio run -t upload
```

手工走一遍闭环（板子烧好之后）：
按 BOOT 键 → 看 LED 单闪（本地反馈）→ 刷新网页看新行「待回应」→
点「回应」→ 最坏 2 秒后板子两下慢闪 → 该行指令变「成功」、操作列变「设备已播放反馈」。

---

## 上板验证清单（第一件事）

| # | 检查 | 期望 | 不符时怎么办 |
| --- | --- | --- | --- |
| 1 | 上电后 1 秒内 LED | 单闪一次 | 完全没亮 → `PIN_LED` 不是 21，逐个试 48/其他，或外接 LED |
| 2 | LED 常亮不灭 | 不应出现 | 低有效 → `LED_ON_LEVEL` 改 `LOW` |
| 3 | 按 BOOT 键 | 立刻单闪，串口 `[btn] 按下 seq=…` | 一次按压打出多条 seq → 加大 `BUTTON_DEBOUNCE_MS` |
| 4 | 串口 `[btn] 上传成功 seq=… HTTP 201` | 201（重发时 200） | 401 → `secrets.h` 的 `INGEST_TOKEN` 与服务端不一致 |
| 5 | 网页出现该行、可信度徽标 | 「NTP 新鲜」 | 「未同步」→ 检查 NTP，事件仍应上报（`t_device_ntp_ms=null`） |
| 6 | 网页点「回应」 | 最坏 2s 后两下慢闪；指令 → 成功 | 30s 后 expired → 设备没在 claim（检查 Wi-Fi / `COMMAND_POLL_MS`） |
| 7 | 网页点「取消」 | 六下快闪 | |
| 8 | 断网按键 12 次再联网 | 上来的事件里带 `queue_dropped`，界面显示「板上曾丢弃 N 次」 | 一条不丢也不记账 → 队列/记账逻辑有问题 |
| 9 | notify 回执里的 `led_pin_note` | `verified` | 仍是 `pin_unverified` → 1、2 两项确认无误后把 `PIN_LED_VERIFIED` 改 1，`FW_VERSION` → `0.3.1` |

---

## 仍未解决 / 下一步

1. **LED 引脚与有效电平未在硬件上确认**（4.5）。这是本周最大的未知，
   已经用 `PIN_LED_VERIFIED` + `led_pin_note` 把"没确认"这件事显式地带到了界面上，
   但真正的确认只能靠上板。
2. **反馈只有 LED**。佩戴场景下更合适的是振动马达或蜂鸣器；
   `button_play_decision()` 已经是"按 decision 播放一段物理图案"的抽象，
   换执行器只需要改这一个函数 + `config.h`。
3. **按键只有一种语义**。现在是"按一下 = 一次待回应事件"，
   还没有长按/双击/组合键。`press_seq` 与 `t_press_uptime_ms` 已经带了做手势识别
   所需的全部信息（间隔可算），但手势判定应该放在板上还是服务端需要单独设计——
   放板上会引入新的幂等键维度，放服务端会让本地反馈变慢。
4. **notify 的 ttl 是 30 秒**，设备离线超过 30 秒的回应必然 expired。
   要不要"离线时排队等设备回来"是个产品问题：现在的选择是**宁可失败可见，
   不要在 5 分钟后突然闪灯吓人**。
5. **事件与指令的关联只做了一层**（`request_id` 最近一次）。
   如果要审计"这一次按键历史上被回应过几次、每次结果如何"，
   现在得去 `commands` 表按 `params_json` 里的 `event_id` 反查；
   够用但不优雅，将来可以加一张 `button_responds` 关联表。
6. **没有多设备视图**。事件列表可以按 `device_mac` 过滤，但界面还没有选择器；
   第4周如果要多台板子同时佩戴，这是第一个要补的 UI。
