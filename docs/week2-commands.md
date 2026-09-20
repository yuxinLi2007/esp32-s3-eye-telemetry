# 第2周：Web 远程采集指令与执行结果反馈

第1周是"设备自己往上推、网页只能看"。第2周加上反方向的一条链路：
**网页点名要一批数据 → 设备照办 → 结果和样本回到网页**，
并且这条链路上任何一环失败（设备离线、领了不跑、跑一半崩、上传掉样、双击重复下发）
都要在界面上有它自己的样子，不允许静默。

延续第1周的三条口径：

- **失败必须可见**：不是"没有报错"就算成功，而是每一种失败都有独立的呈现。
- **单一事实来源**：状态机只在 `server/commands.py` 里有一份实现；
  参数范围只由 `/api/v1/commands/ops` 给出，前端不另抄。
- **派生值不落库**：`duration_ms`、`remaining_ms`、`sample_count_mismatch`
  都是算出来的，不进数据库、不下发给板子。

---

## 第一步：第1周结构分析 —— 哪些模块必须动

| 文件 | 第1周职责 | 第2周改动 | 为什么这么改 |
| --- | --- | --- | --- |
| `firmware/src/main.cpp` | setup + loop 里写满了采集/上报逻辑 | **只加 3 处接线**：`command_begin(fill_snapshot, stream_tick)`、loop 里 `command_poll()`；把原循环体抽成 `stream_tick()` | 要求是"功能逻辑不写在 main"。抽 `stream_tick()` 还有个硬理由：capture 最长 10 秒，这期间如果主循环停摆，连续流就会断——采集等待期必须能回调它 |
| `firmware/src/sensors.h/.cpp` | I2C 加速度 + I2S 麦克风读取 | **不改**（只把读失败如实返回，见下） | 指令采集和连续采集必须读同一套传感器，否则"远程采到的数据"和"图上那条线"不是同一种东西 |
| `firmware/include/config.h` | 采样/上传/缓冲参数 | +`COMMAND_POLL_MS 2000`；`FW_VERSION` → `0.2.0` | 轮询间隔决定"下发到开始执行"的最坏时延，必须是显式常量而不是散在代码里的数字 |
| `firmware/src/uplink.h/.cpp` | （原本写在 main 里） | **新增**：HTTP 统一出口、`json_escape`、ingest JSON 构建 | 指令回执、进度心跳、样本上传都要发 HTTP，各写一份迟早不一致 |
| `firmware/src/command.h/.cpp` | — | **新增**：领取 / 解析 / ping / selftest / capture 真实采集 / 进度心跳 / 失败上报 | 第2周的全部板端逻辑都在这里，main 不碰 |
| `server/db.py` | 建表、写入批次、查询 readings | `batches` 加 `request_id` 列 + 旧库迁移；`query_readings` 支持 `request_id` / `include_command_samples`；新增 `count_command_samples` | 指令采到的样本必须能被单独捞出来，但**默认视图不能混进去**（否则连续流图上会出现一段采样率完全不同的线），同时要把"过滤掉多少条"报出来 |
| `server/commands.py` | — | **新增**：状态机 + 幂等 + 结算 + 协议编解码（约 900 行） | 状态机只此一份。板端与测试脚本都 `import` 它的编解码函数，不各写一套 |
| `server/app.py` | ingest / readings / status / control | +7 个指令端点；ingest 接受并校验 `request_id`；`/status` 顺带 `sweep()` | 惰性结算：界面每 2 秒轮询一次 status，于是"到点该判超时"最迟 2 秒后发生，不需要后台线程 |
| `server/static/index.html` | 图表 + 指标卡 + 采集开关 | +指令面板 DOM/CSS；末尾暴露 `window.DASH`（`$`/`api`/`el`/`fmtTime`/`ctlHeaders`/`ctlToken` getter…） | 两个面板要共用同一套工具与**同一个 CONTROL_TOKEN**。`ctlToken` 用 getter 暴露：它是会被 401 流程改写的局部变量，赋值副本的话两个面板各持一份令牌，很快就对不上 |
| `server/static/commands.js` | — | **新增**：指令面板全部逻辑（约 460 行） | |
| `server/tools/ui_check.mjs` | 第1周前端回归 | 内联脚本正则改**非贪婪**；DOM 桩补 `classList`/`readyState`/`confirm`；+第2周面板检查与防抖断言 | 页面现在有两个 `<script>`，贪婪匹配会把 `</script><script src=…>` 一起吞进代码里 |
| `server/tools/command_sim.py` | — | **新增**：假设备模拟器 + 故障注入，打真实 HTTP | 见第六步 |
| `server/test_commands.py` | — | **新增**：80 个单测 | |

**没有改的**：`sensors.*` 的读取路径、图表绘制、时间戳可信度口径、入库鉴权。

---

## 第二步：`request_id` 生成规则与状态机

### 2.1 request_id

```
req_<创建时刻 epoch_ms 的 9 位定宽 base36>_<6 位十六进制随机>
例：req_0mu9l1m7o_41bace
```

四条理由，每条都对应一个具体的坑：

1. **前缀 `req_`**：日志里 grep 得到、念得出来，不会和 `boot_id`（8 位大写十六进制）混。
2. **9 位定宽 base36 时间片**：定宽才能让**字典序 == 时间序**，翻指令列表不必先解析再排序。
   （不定宽的话 `req_1x_` 会排在 `req_2_` 后面。）
3. **6 位随机片用 `secrets` 而不是 `random`**：request_id 会出现在 URL 和日志里，
   可预测就等于可枚举——而 `/api/v1/commands/{rid}` 是能查到结果的。
4. **权威时间只有 `t_created_ms` 一列**。id 里的时间片只是给人看的线索，
   任何逻辑都不许反解它来当时间用，否则派生值就变成了第二个事实来源。
   `command_sim.py` 里有一条断言专门盯这件事：`id 时间片 == t_created_ms`。

碰撞处理：`request_id` 是主键，撞了就换一个，最多试 5 次；5 次都撞返回 500 `id_collision`。
概率是每毫秒 16⁶ 分之一，但**绝不能让一次碰撞变成一个静默的 500 或者一条写错的指令**。

### 2.2 状态机（8 个状态，5 个终态）

```
                          POST /api/v1/commands
                                     │
                                     ▼
                            ┌────────────────┐   cancel（只有此状态可撤）  ┌─────────────┐
                            │    pending     │──────────────────────────▶│  cancelled  │■
                            │  等待设备领取   │                            └─────────────┘
                            └────────────────┘
                               │            │
      ttl_ms(30s) 到点无人领取 │            │ 设备 POST /commands/claim（attempts+1）
                               ▼            ▼
                        ┌────────────┐  ┌────────────┐
                        │  expired   │■ │  claimed   │
                        │ 超期未领取  │  │ 设备已领取  │
                        └────────────┘  └────────────┘
                                          │        ▲
                       回执 state=running │        │ 静默 > timeout_ms
                                          ▼        │ 且 attempts < max_attempts
                                     ┌─────────┐   │ → 清掉领取痕迹、requeues+1、
                                     │ running │───┘   回到 pending 等下一次领取
                                     │设备执行中│
                                     └─────────┘
                                       │      │      │
             回执 state=done ──────────┘      │      └────────── 回执 state=failed
                        ▼                     ▼                        ▼
                 ┌───────────┐         ┌───────────┐            ┌───────────┐
                 │   done    │■        │  timeout  │■           │  failed   │■
                 │   成功     │         │  执行超时  │            │  设备报错  │
                 └───────────┘         └───────────┘            └───────────┘
                                              ▲
                     执行中静默 > timeout_ms ───┘（RUNNING 一律不重排队）
                     或 attempts 已用尽（默认 max_attempts=2）

   ■ = 终态。终态不可再变：迟到/重放/设备重启后补发的回执一律 409 already_terminal
```

代码里的允许迁移表（`TRANSITIONS`）：

| from | to |
| --- | --- |
| pending | claimed / cancelled / expired |
| claimed | running / done / failed / timeout / **pending**（重排队） |
| running | running（进度心跳）/ done / failed / timeout / **pending** |
| done · failed · expired · timeout · cancelled | ∅（终态） |

> 表里允许 `running → pending`，但 `sweep()` **刻意不走这条路**：
> 设备已经开始执行，静默可能只是网络抖动；自动重跑一次采集会得到
> "两批数据对不上号"的更坏结果。判成 timeout 让人看见、由人决定是否重发，更诚实。
> 只有 `claimed`（领了却从没上报过进度）才自动重排队。

### 2.3 超时锚点：为什么不是"领取时刻"

```python
deadline = t_created_ms + ttl_ms        # pending：等设备来领
deadline = t_last_event_ms + timeout_ms # claimed/running：等下一个动静
```

锚点是**最后一次有动静的时刻**。一次 10 秒的大采集如果锚在领取时刻，
固定 20 秒窗口就会把正常执行误判成超时——那是最坏的一类误报（用户会去怀疑设备坏了）。
所以进度心跳（`state=running` + `progress`）不只是给界面画进度条，它同时**续命**。

`timeout_ms` 也随参数增长：`capture` = `20000 + 2 × n × interval_ms`。
`ttl_ms` 固定 30 秒（约 15 次轮询），到点没人领就是 `expired`——
界面据此区分"设备离线 / MAC 写错 / 固件太旧不支持指令通道"和"设备执行失败"。

### 2.4 惰性结算（sweep）

没有后台线程。`sweep()` 在**每一个读写指令的入口**被调用
（claim / create / cancel / get / list / stats / apply_result），
于是"界面上看到的状态"永远不会是"一个已经过期的旧状态"。
界面每 2 秒轮询 `/api/v1/status`，所以最坏 2 秒后结算发生。
---

## 第三步：后端 —— 指令下发与状态追踪

### 3.1 端点一览

| 方法 | 路径 | 鉴权 | 作用 |
| --- | --- | --- | --- |
| GET | `/api/v1/commands/ops` | 无 | 指令白名单 + 参数范围 + `max_live_per_device` + `max_capture_duration_ms`。**前端按它渲染表单** |
| POST | `/api/v1/commands` | `X-Control-Token` | 下发。新建 201；幂等命中 200 + `x-deduped: 1` |
| GET | `/api/v1/commands?device_mac=&state=&op=&limit=` | 无 | 列表 + 全局 `stats`（8 个状态各自计数 + `live`） |
| GET | `/api/v1/commands/{request_id}?with_events=` | 无 | 单条详情 + 事件时间线 |
| POST | `/api/v1/commands/{request_id}/cancel` | `X-Control-Token` | 撤销（仅 pending） |
| POST | `/api/v1/commands/claim` | `X-Ingest-Token` | **设备领取**，纯文本进出 |
| POST | `/api/v1/commands/{request_id}/result` | `X-Ingest-Token` | **设备回执**，JSON |
| POST | `/api/v1/ingest` | `X-Ingest-Token` | 第1周的老路，第2周多了可选 `request_id` |
| GET | `/api/v1/readings?request_id=` | 无 | 只看某条指令采到的样本 |

> 路由注册顺序要紧：`/commands/ops`、`/commands/claim` 必须排在 `/commands/{request_id}`
> 之前，否则 `ops`、`claim` 会被当成 request_id 匹配掉。

### 3.2 幂等：防抖不能只靠前端

唯一索引 `uq_commands_token (device_mac, op, client_token)`。

一次用户意图一个 `client_token`：双击、超时重试、断网重发都带**同一个** token，
服务端只会产生一条指令，第二次提交返回 200 + `x-deduped: 1` + 原来那条。
前端禁用按钮挡不住刷新页面、多标签页和直接 `curl`，**唯一索引才是最后一道**。
并发双击也安全：撞唯一键时回滚、查出原来那条返回，而不是报 500。
（`client_token` 为 NULL 时 SQLite 不参与唯一约束，所以"不带 token 的下发"可以重复。）

### 3.3 在飞上限

`MAX_LIVE_PER_DEVICE = 8`，超出返回 **429 `too_many_live`**，消息里写明理由：
设备是串行执行的，再排下去只是让队列变长。没有它，一次连点就能排下几百条指令，
板子会老老实实执行几分钟，而界面上只显示"一堆等待中"——用户既停不下来也不知道为什么。

### 3.4 回执校验链（顺序即优先级）

| 情况 | HTTP | `error.code` |
| --- | --- | --- |
| 指令不存在 | 404 | `not_found` |
| `state` 不是 running/done/failed | 400 | `bad_state` |
| 指令已是终态 | 409 | `already_terminal` |
| `boot_id` 与领取者不符（设备重启过） | 409 | `boot_mismatch` |
| `device_mac` 与指令不符 | 409 | `mac_mismatch` |
| 还没被领取就来回执 | 409 | `not_claimed` |
| `progress` 不是 0~100 整数 | 400 | `bad_progress` |
| `progress` 比已记录的更小（乱序/重放） | 409 | `progress_regressed` |
| `failed` 不带 `error_code` | 400 | `missing_error_code` |

统一出口：`{"ok": false, "error": {"code": "...", "message": "..."}}`。
**code 给程序看**（前端据此决定要不要提示重新输入令牌），**message 给人看**，
两者都返回，前端不必去解析中文句子。

### 3.5 样本双计数：把"上传掉样"变成可见的事实

```
n_samples         只由 ingest 那条路写入（服务端真正收到了多少条）
n_samples_device  设备在回执里自称采了多少条
sample_count_mismatch = (两者都非空且不相等)   ← 派生，不落库
```

任何一方单独都证明不了"数据在上传途中丢了"，只有**两个数并排对不上**才是真信息。
所以界面上样本数一格永远显示 `服务端实收 / 设备自报`，不一致时标 ⚠。
光看 `state=done` 会把丢数据说成一切正常。

### 3.6 事件时间线

每次状态迁移写一行 `command_events(request_id, t_server_ms, from_state, to_state, actor, detail)`，
`actor` 形如 `web` / `device:<boot_id>` / `server` / `ingest`。
和 `control_log` 一样：**当前状态是派生事实，"这个状态是怎么来的"必须有原始记录**，
否则出了问题只能靠猜。`ingest` 挂样本时写一条同状态注记（from == to），
于是"样本是什么时候到的"也在同一条时间线上。

---

## 第四步：设备端 —— 指令接收与真实采集

### 4.1 领取协议是纯文本，不是 JSON

```
请求  POST /api/v1/commands/claim     X-Ingest-Token
      94:A9:90:1C:6F:D4|A1B2C3D4|0.2.0            <- mac|boot_id|fw_version
应答  200 text/plain
      req_0mu9l1m7o_41bace|capture|interval_ms=20;n=6|20240
                                                    <- request_id|op|k=v;k=v|timeout_ms
      或  none                                       <- 没有可领的
```

> **参数段是固定 4 段里的第 3 段，没有参数时必须是空串，不能省略。**
> `ping` / `selftest` 没有参数，应答就是 `req_xxx|ping||10000`——中间那两个竖线挨在一起。
> 板端 `split_claim` 按 `|` 切出固定 4 段，真机联调时旧版要求"4 段全部非空"，
> 于是这两条指令领到手里就解析失败、永远不回执，在服务端只能等成 `timeout`。
> 而服务端的 `decode_claim` 对空参数段是宽松的，所以 104 个单测和仿真器全绿，
> 问题只在真机上暴露。现在两侧都有测试钉住这个格式：
> `test_claim_of_paramless_op_keeps_four_fields_with_empty_params`（服务端口径）
> 与 `command_sim.firmware_split_claim`（固件口径，每次领取都过一遍）。

板端不解析 JSON：多引一个库、多几百字节 RAM、多一个可能失败的分支，
而这里要传的东西完全可以用一行分隔文本表达。
**能这么干的前提**是 op / 参数名 / 参数值都在服务端白名单里被校验过，
分隔符 `|` `;` `=` 不可能出现在任何字段里，因此编码不会被注入打断。

派生值 `duration_ms` **不下发**：板端自己会算，两边都算就是两份事实。
参数按名字排序编码，两端不必约定顺序。

`boot_id` 让领取具备幂等性：同一次启动重复来领，拿到的是同一条，
不会多发一条指令、也不会把 `attempts` 加两次（板子重启换了 boot_id 才算新的一次尝试）。

### 4.2 三个 op 都执行**真实**动作

| op | 板端做什么 | 结果体关键字段 |
| --- | --- | --- |
| `ping` | 什么都不采，只回报自身状况 | `uptime_s` `rssi_dbm` `free_heap` `ring_count` `seq` `collect_enabled` |
| `selftest` | 实读加速度合矢量与麦克风帧数，逐通道给通过/不通过 | `mag_avg` `mag_tol` `accel_verdict` `mic_verdict` `n_frames` |
| `capture` | 按 `interval_ms` 真实读传感器 `n` 次，样本带 `request_id` 走 ingest 入库 | `n_requested` `n_sampled` `spl_avg_db` `mag_avg` `last_batch_id` |

`capture` 的顺序是刻意的：**先把样本上传入库，再回执 done**。
这样"指令采集的数据"和"连续流数据"用的是同一套溯源口径
（批次、seq、NTP 状态、服务端接收时刻），不需要为指令另建一条数据通路。

采集等待期调用 `stream_tick()`：主循环不停摆，连续流不断线。
单次采集上限 10 秒也不是拍的数——`RING_CAPACITY=240 @20Hz = 12 秒`，
压在 10 秒内缓冲就一定不会溢出、不会丢连续流的样本。

### 4.3 失败必须带 error_code

| `error_code` | 什么时候报 |
| --- | --- |
| `accel_i2c` | 全部采样的加速度读取都失败（I2C 挂了） |
| `sensor_read_failed` | 部分采样失败，附上失败计数 |
| `mic_no_output` | I2S 读到的帧全是 0（麦克风没输出） |
| `upload_failed` | 样本上传失败（附上 HTTP 状态） |
| `deadline_exceeded` | 采集没在 `timeout_ms` 内跑完，主动放弃并如实上报 |
| `unsupported_op` | 领到了这个固件不认识的 op |

`deadline_exceeded` 值得单说：板子自己算得出会不会超期，
**主动放弃并上报**远好过让服务端判 timeout——后者只能告诉用户"没动静"，
前者能告诉用户"采到第几条时超期了"。

### 4.4 顺带修掉的一处不诚实

第1周加速度读取失败时写的是 `0.000`。在图上那是一个**看起来合法的读数**
（合矢量 0，等于"设备在自由落体"），比报错更难发现。
现在写 NaN，JSON 里序列化成 `null`，前端按"缺这一点"处理。
连续流和指令流都适用。

---

## 第五步：前端 —— 按钮与状态展示

`server/static/commands.js`，复用 `index.html` 暴露的 `window.DASH`。

- **按钮和参数范围全部来自 `/api/v1/commands/ops`**：
  `input.min/max`、提示文字里的上限都是服务端给的。服务端改了范围，界面跟着变，
  不会出现"前端能填、后端拒绝"的错位。
- **双层防抖**：
  1. `busyOp` —— 下发期间所有按钮和参数框禁用，按钮文字变成 `ping …`；
  2. `client_token` —— 同一次点击意图（MAC+op+参数相同）复用同一个 token，
     服务端唯一索引兜底。
- **401 现问令牌**：服务端设了 `CONTROL_TOKEN` 时，下发会 401，
  此时 `prompt` 要一次令牌并记住，与第1周采集开关**同一个存储键**，用户只需输一次。
  （网页里不可能预置密钥：HTML 本身是要发出去的。）
- **状态展示**：8 种徽章（每种状态一种颜色）、进度条、剩余时间、重排队次数、
  排队/执行耗时（hover 显示 `queue_ms` / `exec_ms`）、样本数 `服务端 / 设备` 并排 + ⚠。
- **失败各有各的样子**：
  `expired` 说"ttl 内没有设备来领（离线/MAC 写错/固件太旧）"；
  `failed`/`timeout` 显示 `[error_code] message`；
  被幂等去重时说"重复提交已被服务端去重，仍是同一条 req_xxx"——
  这不是错误，但用户以为发了两条、实际只有一条，界面若沉默就会让人怀疑按钮坏了。
- **详情**：结果体键值、事件时间线（谁在什么时候把状态从 A 改成 B、为什么）、
  这条指令采到的样本表。
- **撤销**：`confirm` 后调 cancel，只有 pending 能撤；已被领取的不能抽走。
- **状态不自己记**：按钮可用性、进度、耗时全部由 2 秒轮询回来的数据决定。
  自己记一份状态的话，另一个标签页下发的指令在这里就看不见了。

---

## 第六步：鲁棒性测试

三层，各测各的，不互相替代。

### 6.1 单元/契约层 —— `pytest`

```
cd server
python -m pytest test_commands.py test_ingest.py -q
→ 104 passed
```

覆盖：状态机迁移合法性、request_id 格式与 base36 定宽、参数校验矩阵、
幂等（含并发）、sweep 的四种分支、终态不可改、回执校验链、
`to_dict` 的派生字段、`db.py` 的旧库迁移（用裸 SQL 模拟第1周写入的行）。

### 6.2 端到端层 —— `tools/command_sim.py`（假设备 + 故障注入，打真实 HTTP）

```
→ 162 断言全部通过（约 35 秒，三个 30 秒级场景并发点火）
```

**为什么不用 TestClient**：第1周吃过这个亏——TestClient 串行执行，
把 FastAPI 跨线程分派的问题盖住了，测试全绿、真机必现。
指令通道是"服务端 / 设备 / 网页"三方异步交互，时序问题只有在真实 socket 下才露出来。

| 场景 | 制造的失败 | 断言的事实 |
| --- | --- | --- |
| S0 | — | ops 白名单/范围由服务端给出；request_id 格式、时间片 == `t_created_ms`、字典序 == 时间序 |
| S1 | — | 正常 capture 全流程；领取文本不带 `duration_ms`；事件时间线完整；按 request_id 能捞回样本；默认视图排除指令样本但如实报数 |
| S2 | 双击、4 线程并发同 token | 只产生 1 条；恰好一个 201 其余 200；`x-deduped: 1`；不同 token 必须是两条 |
| S3 | n=0/201、interval=9/1001、n×interval=15000、n="abc"、n=3.5、白名单外参数、op=`reboot`/`DROP TABLE`/``、MAC 三种非法、token 带空格 | 每一条都拒，且 `error.code` 可判别（`bad_param`/`capture_too_long`/`unknown_op`/`bad_op`/`bad_mac`/`bad_token`）；小写 MAC 被规范化而不是被拒 |
| S4 | 第 9 条在飞指令 | 429 `too_many_live`，消息里写明理由 |
| S5 | 设备报 `accel_i2c` / `mic_no_output`；failed 不带 error_code | 终态 failed + code/message 原样保存；失败时的 progress 被保留（看得出死在哪一步）；不带 code 被拒且指令状态没被写坏 |
| S6 | 未领取就回执、boot_id 不符、MAC 不符、progress 回退、progress=150、未知 state、终态后再回执、对不存在的指令回执 | 全部 409/400/422/404，且**迟到回执没有覆盖已成的结论** |
| S7 | 设备自报 6 条、只上传成功 3 条 | `state=done` 但 `sample_count_mismatch=True`；列表接口也带这个标记；ingest 挂未知/非法 request_id 被拒（不产生无主样本） |
| S8 | 撤销 pending / 重复撤销 / 撤销 claimed / 撤销不存在的 | 只有 pending 能撤；撤销后设备来领**领不到**；其余 409 `not_cancellable` / 404 |
| S9 | 领取后彻底静默 | 10 秒后重排队（`requeues=1`，领取痕迹被清干净）→ 再领（`attempts=2`）→ 再静默 → `timeout`，`requeues` 停在 1 **没有无限重发**；超时后姗姗来迟的 done 被拒 409 |
| S9b | 上报过一次进度后静默 | 直接 `timeout`，`requeues=0`（RUNNING 不重排队），事件理由里带当时的 progress |
| S10 | 设备从头到尾不出现 | 30 秒后 `expired`，`attempts=0`，事件写明 `ttl_ms=30000`；离线设备后来上线也领不到这条 |

设计上的两个细节：

- **每个场景一台设备**。`claim` 取的是"该 MAC 最早的一条 pending"，
  共用一台设备的话 A 场景会把 B 场景的指令领走，断言就成了抓阄。
- **每次运行一个 `RUN` 标识**，MAC 与 client_token 都带上它。
  否则第二次跑脚本时，上一次留在库里的 pending 会被这次领走、
  固定 token 会被判成重复提交——测出来的是幂等而不是下发。

### 6.3 前端回归层 —— `tools/ui_check.mjs`

```
node tools/ui_check.mjs --origin http://127.0.0.1:8001
```

在 node 里用最小 DOM 桩跑真实前端代码，对着真实服务端数据断言。
第1周的部分（报警等级、分段/缺口判定、指标卡、采集开关按钮可用性）原样保留；
第2周新增：按钮清单与禁用态、参数框范围（来自服务端）、`cmd_stats`、表格徽章，
以及**防抖断言**：MAC 填好之后连点 3 次 ping，数 `POST /api/v1/commands` 的次数，
必须是 1；不是 1 就以非零码退出（这个检查是给 CI 看的，不是给人肉看的）。

### 6.4 固件

`pio run` 通过：RAM 17.6% / Flash 27.6%（第1周是 RAM 16.9% / Flash 25.8% 量级）。

---

## 怎么跑

```powershell
# 1) 服务端（用独立库，别污染真实数据）
cd D:\esp32-s3-eye-telemetry\server
$env:TELEMETRY_DB = "D:\esp32-s3-eye-telemetry\.scratch\sim.db"
python -m uvicorn app:app --port 8002

# 2) 端到端鲁棒性验证（另开一个终端）
python tools\command_sim.py --url http://127.0.0.1:8002     # 加 --fast 跳过 30 秒级场景

# 3) 前端回归（对着 8001 那个实例，先跑 tools\fault_inject.py 灌入缺陷数据）
node tools\ui_check.mjs --origin http://127.0.0.1:8001

# 4) 单元测试
python -m pytest test_commands.py test_ingest.py -q
```

真机：`CONTROL_TOKEN` 若在服务端设了，网页下发时会弹一次输入框；
`INGEST_TOKEN` 走固件的 `config.h`。板子最坏 `COMMAND_POLL_MS`(2s) 后领取指令。

---

## 仍未解决 / 下一步

1. **单设备串行执行**：一条 capture 在跑时，后面的指令只能排队。
   要做并发就得在板端加任务与互斥，代价是环形缓冲争用。
2. **没有 WebSocket/SSE**：状态靠 2 秒轮询。指令条数多了之后，
   `/api/v1/commands` 的响应体会变大（`to_dict` 每次算一遍派生字段）。
3. **CONTROL_TOKEN 存在 localStorage**：XSS 下会被读走。
   真要上公网得换成短时效会话或服务端代理。
4. **sweep 是全表扫 live 行**：在飞指令上限 8/设备，规模小时无所谓，
   设备上百台时要换成"按 deadline 排序取前 N 行"。
5. **`running → pending` 在迁移表里合法但从不自动走**：
   将来若要支持"人工重发执行中的指令"，需要先想清楚两批样本怎么区分。
6. **capture 期间连续流靠环形缓冲扛**：上限 10 秒是从 12 秒缓冲倒推的，
   改 `RING_CAPACITY` 或采样率时必须同步改 `MAX_CAPTURE_DURATION_MS`，
   这个耦合目前只写在注释里，没有测试守着。