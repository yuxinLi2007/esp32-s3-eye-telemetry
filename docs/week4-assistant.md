# 第4周：自然语言助手 —— 让大模型当"意图翻译官"

一句话 -> 识别意图 -> 映射成「查看历史数据」或「请求一次新采集」 -> 调用已有的 VPS 接口。

本周不新建数据通道，不新建状态机。助手是**编排层**：它复用第1周的
`/api/v1/readings`、第2周的 `/api/v1/commands(op=capture)` 与整套指令状态机、
第3周的按键/回执机制。新东西只有三样：意图翻译、白名单护栏、结构化错误契约。

---

## 1. 为什么不让模型直接执行

模型能编。它编出来的 MAC、参数、动作，一旦直接落到指令表，就是一次**真实的越权操作**。
所以本周的第一条设计口径是：

> **模型只翻译，不执行。它唯一的产出是结构化意图（intent + slots），
> 真正的动作由服务端用白名单校验后，调用已有接口完成。**

这条线划在哪里，决定了整个模块的形状：

| 阶段 | 谁负责 | 产物 |
|---|---|---|
| 理解人话 | 大模型（可选）/ 规则引擎（兜底） | `intent` + `slots` + `reason` |
| 校验意图 | 服务端 `assistant._sanitize_llm()` | 受控 Intent（设备必须过白名单） |
| 执行动作 | 服务端 `assistant.execute_*()` | 调用已有接口 |
| 生成回复 | 服务端事实模板 | `answer`（不让模型复述数字） |

**为什么回复也不让模型写**：模型复述数字会失真。前3周所有取舍都在拒绝"数字失真"
（时间戳可信度分级、seq 缺口判定、指令状态审计），没道理在第4周把这条底线交给模型。

---

## 2. 两个动作 = 两条接口路径

| intent | 用户说法 | 走哪个已有接口 | 时间语义 |
|---|---|---|---|
| `query_history` | "查看上次"、"看看最近50条" | `GET /api/v1/readings` | **保留旧 `t_server_recv_ms`**，绝不改时间 |
| `request_capture` | "重新采集"、"现在采一次" | `POST /api/v1/commands` op=capture | **等新样本**，`t_server_recv_ms` 晚于指令下发时刻 |

这两件事在用户嘴里可能只差一个字（"看"vs"采"），但时间语义完全相反：

- 把"查看"当成"采集" -> 白扰动设备，还可能在连续流里插一段指令样本；
- 把"采集"当成"查看" -> 用户以为拿到了新数据，实际是旧的，**假数据比没有数据更坏**。

所以判不准时的默认动作是 `clarify`，不是"挑一个更可能"的。

### 2.1 "查看上次"到底返回哪一条

`query_history` 识别到"上次 / 上一次 / 刚才 / 最新 / 最后"时，把 `limit` 收成 1，
返回**库里已存在的最后一条**，并把它的 `t_server_recv_ms`、`t_device_ms`、
`t_trust` 原样摆出来。前端不做任何时间替换。

### 2.2 "重新采集"为什么要等设备回执

不能"下发指令 -> sleep 2 秒 -> 查库"。网络抖动时这会拿旧样本冒充新样本，
恰好就是本周测试要防的错。正确做法：

```
POST /api/v1/commands(op=capture)   -> 拿 request_id
poll GET /api/v1/commands/{rid}     -> 直到进入终态
GET  /api/v1/readings?request_id=.. -> 取本次指令产生的样本
```

等待窗口默认 `ttl_ms + timeout_ms + 1s`（capture 默认约 71 秒）。窗口用尽但指令**仍在飞**时，
返回 `device_no_response`（不是 `device_timeout`）——它可能下一秒就回执，谎称超时是不诚实的。

---

## 3. 意图翻译：LLM 与规则双引擎

```
engine=auto   有 OPENAI_API_KEY -> 调模型；无 key / 模型出错 / 返回垃圾 -> 规则兜底
engine=rules  强制离线规则，不联网（测试与断网场景用这个）
engine=llm    只试模型；失败仍然降级到规则，不会把功能打挂
```

**规则引擎不是"临时凑合"**，它是：
1. 无 key / 模型不可用时的可用性保证；
2. 模型输出的**安全对账基准**（见 3.3）；
3. 一个完全可离线、可确定性测试的意图基线。

### 3.1 模型契约

系统提示词（`assistant.LLM_SYSTEM_PROMPT`）规定模型只能输出：

```json
{"intent": "query_history|request_capture|clarify|reject",
 "device_mac": "AA:BB:CC:DD:EE:FF 或 null",
 "slots": {"limit": 50, "n": 40, "interval_ms": 50},
 "reason": "一句话依据"}
```

调用参数：`temperature=0`、`response_format={"type":"json_object"}`。
兼容端点不认 `response_format` 时自动去掉该参数重试一次（不能因为一个可选参数
就把整条 LLM 通道判死）。

### 3.2 模型输出按"不可信输入"处理

`_sanitize_llm()` 是模型越权的唯一闸门：

- `intent` 必须在白名单里；
- `device_mac` 要么为空、要么必须命中白名单，否则**整条意图改判 `reject/forbidden_device`**；
- `slots` 只保留 `limit / n / interval_ms` 三个已知键，多出来的一律丢弃。

而且这还不是最后一道：`_ask_inner()` 在执行动作前**再校验一次**解析结果里的设备。
安全不能依赖"上游一定守规矩"——即使 `parse_llm` 被替换成什么都挡不住的实现也一样拒绝。

### 3.3 规则 vs 模型冲突 -> 强制澄清

`_decide_engine()` 的对账优先级：

1. 规则判定 `reject`（越界/不支持写操作）-> **规则优先**，模型再客气也不能把越权洗合法；
2. 规则与模型给出两个**不同动作** -> **强制 `clarify`** 并附诊断。
   这是最危险的一类错（"查看"↔"采集"互翻），谁都不许替用户拍板；
3. 模型说含糊、规则却高置信命中动作 -> 按规则执行，避免过度追问。

### 3.4 配置项

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | 空 | 不设 = 规则引擎，功能不失效 |
| `OPENAI_MODEL` | `gpt-4o-mini` | 模型名 |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | 兼容端点（可指向本地/自建网关） |
| `ASSISTANT_LLM_TIMEOUT_S` | `8` | 模型调用超时 |
| `ASSISTANT_LLM_ENABLED` | `1` | 设 `0` 彻底关闭模型通道 |
| `DEVICE_ALLOWLIST` | 空 | 逗号分隔；**设了就是权威白名单** |

### 3.5 模型降级的五种状态：不是每个"走规则"都叫故障

`trace.llm_status` 把"模型通道这次怎么了"写成五值之一，前端各给一个 chip：

| llm_status | 含义 | chip | 算故障吗 |
|---|---|---|---|
| `used` | 模型应答了（其意图是否被采纳另按对账规则） | 绿 | 否 |
| `not_configured` | 没设 `OPENAI_API_KEY`，按默认走规则 | 中性 | **否**（本地默认形态） |
| `disabled` | `ASSISTANT_LLM_ENABLED=0`，人为关掉 | 中性 | 否 |
| `off` | 调用方指定 `engine=rules`，模型通道没被调用 | 中性 | 否 |
| `degraded` | 配了 key 但调用失败（网络/超时/401/垃圾输出） | 红 | **是**，原因在 `trace.llm_error` |

只有 `degraded` 才是真降级：模型本该说话却没说成。没配 key 不是降级——
那是本项目的离线默认形态，界面上不画红、不吓唬人；而真的调用失败时，
红色 chip 会把失败原因（异常类型+消息）原样摆出来，符合"失败必须可见"。

---

## 4. 白名单：越界的硬边界

```
DEVICE_ALLOWLIST 已设置 -> 只认这一份（公网部署口径）
未设置                 -> 用 batches 里"服务端见过的设备"（本地零配置）
```

关键点：**"见过"来自服务端自己的入库记录，不是用户输入**。所以
"控制别人的设备"在任何配置下都命中 `forbidden_device`。

越界请求**不返回设备清单**——越权尝试不该换来一份可供枚举的白名单。
多台设备且用户没点名 -> `need_device`，要求澄清；句子里点名 MAC 与请求参数
`device_mac` 不一致 -> 拒绝（不悄悄换设备）。

---

## 5. 结构化错误契约

`POST /api/v1/assistant/ask` **永远返回 HTTP 200**（鉴权失败除外），body 是同一个信封。
无论越界、含糊、无数据还是设备不回，调用方都不需要 try/except，也不会拿到一个 500。

```jsonc
{
  "ok": false,                     // 动作是否真的完成
  "intent": "clarify",             // query_history | request_capture | clarify | reject
  "engine": "rules",               // 调用方请求的引擎
  "confidence": 0.5,               // 意图置信度 0~1
  "device_mac": null,              // 实际作用的设备；拒绝/澄清时为 null
  "answer": "请说明要操作哪台设备。", // 面向人的一句话
  "action": null,                  // 走了哪个已有接口、参数、指令状态
  "data": null,                    // 查询/采集结果（readings、is_new_sample 等）
  "error": { "code": "need_device", "level": "info", "message": "..." },
  "trace": { "engine_chain": ["rules"], "engine_used": "rules",
              "llm_status": "not_configured", "elapsed_ms": 3 }
}
```

`answer` 里的数字**全部由服务端从库里取出来拼**，不让模型复述——模型会把 1790058919891
写成 1790058920000 而不自知。模型只负责产出 `intent + slots`，执行与措辞都在服务端。

| 错误码 | 级别 | 什么时候出现 |
|---|---|---|
| `ambiguous_request` | info | 含糊指令（"帮我弄一下"），必须让用户二选一 |
| `unrecognized_request` | info | 完全没匹配到已知动作，绝不默认采集 |
| `need_device` | info | 服务端看到多台设备但用户没点名 |
| `unknown_device` | info | 白名单为空且库里没有任何批次 |
| `forbidden_device` | warn | 设备不在允许名单内（越界），拒绝且**不下发指令** |
| `unsupported_action` | warn | 要求停止采集/重启/改密码等两个动作之外的写操作 |
| `bad_params` | warn | 参数非法且无法安全夹取 |
| `no_data` | info | 查询命中 0 条。不是崩溃，但要如实说明 |
| `device_unreachable` | error | 指令在 ttl 内没有设备来领取（离线或 MAC 写错） |
| `device_no_response` | error | 等待窗口用尽但指令**还活着**（不是超时，别谎报） |
| `device_timeout` | error | 设备领了却一直不回结果 |
| `device_failed` | error | 设备明确回报失败 |
| `command_cancelled` | warn | 指令被撤销 |
| `too_many_live` | warn | 该设备在飞指令已达上限（8 条） |
| `internal_error` | error | 未预期异常已兜底，绝不把 500 抛给用户 |

**为什么 `device_no_response` 和 `device_timeout` 要分开**：等待窗口是调用方给的
（默认 `ttl_ms + timeout_ms + 1s ≈ 71s`，前端默认填 12s）。窗口到了指令可能还在飞，
这时说"超时"就是在编——它下一秒可能就回执。于是前者返回 `action.request_id` 和
`action.queue_url`，让调用方拿着 request_id 继续查；后者才是状态机真的结算成了 timeout。

## 6. 参数夹取：说"采 500 次"不会失败，也不会真采 500 次

```
fit_capture_params()  -> 从 commands.OPS["capture"]["params"] 读 n / interval_ms 的 lo、hi
fit_query_limit()     -> limit 夹到 [1, 500]
```

只从 `commands.OPS` 读一份范围，前端不另抄——改一边忘一边是迟早的事。
夹取的每一步都写进 `action.adjustments` / `data.adjustments`，例如：

```
"n=500 超过上限，已夹到 200"
"n×interval_ms=10000 超过单次采集上限 10000 ms，n 已降到 200"
```

用户说"采 500 次"想表达的是"尽量多采"，直接报错不如夹到上限并明说。
但时长上限必须真的压住：采集期间连续流全靠板上 12 秒环形缓冲扛着
（`MAX_CAPTURE_DURATION_MS=10000`，`n×interval_ms` 不许越过它）。

## 7. 接口

| 方法 | 路径 | 鉴权 | 作用 |
|---|---|---|---|
| GET | `/api/v1/assistant/info` | 无 | 能力自述：两个动作、当前引擎、错误码表、允许设备 |
| POST | `/api/v1/assistant/ask` | `X-Control-Token` | 一句话 → 意图 → 调已有接口 → 结构化结果 |

`ask` 请求体：

```jsonc
{
  "text": "重新采集一次",          // 必填，≤500 字
  "device_mac": null,             // 可选；留空=唯一已知设备
  "engine": "auto",              // auto | rules | llm
  "wait_ms": 12000,               // 可选，仅采集用；0~120000
  "client_token": null            // 可选，写操作的幂等键
}
```

`engine` 三种取值：

- `auto`（默认）：有 `OPENAI_API_KEY` 就用模型；无 key / 模型报错 / 返回垃圾 → 规则兜底；
- `rules`：强制离线规则，**一个网络包都不发**（断网演示与自动化测试用这个）；
- `llm`：只试模型，失败仍然降级到规则，不会把功能打挂。

## 8. 前端

主界面新增「自然语言助手（第4周）」面板（`static/index.html` 的 `nl_*` 元素 +
`static/assistant.js`）：

- 输入框 + 引擎下拉 + 目标设备 + 最长等待（秒）；
- 示例问题按钮（"查看上次数据" / "重新采集一次" / "帮我弄一下" / "停止采集"）；
- 结果区打出 intent、引擎、接口、错误码、耗时 chip；
- **写操作（request_capture）的结果框标黄**，提醒用户这会扰动设备；
- 数据表直接显示 `t_server_recv_ms`，并明确标注"旧数据 / 本次新样本"。

## 9. 两组必测场景怎么验

### 场景一：新旧数据

1. 先让设备上传一批数据（或跑 `nl_demo.py` 自动注入一批"旧数据"）；
2. 问 **"查看上次"** → 期望 `intent=query_history`、`data.is_new_sample=false`、
   `latest.t_server_recv_ms` **等于**旧数据入库时那一刻，且指令表 `total` 不增加（只读）；
3. 问 **"重新采集一次"** → 期望 `intent=request_capture`、
   先产生一条 `capture` 指令、设备回执后 `data.is_new_sample=true`，
   且新样本 `t_server_recv_ms >= capture_started_ms`，明显晚于上一步的旧时间戳。

判定要点：**"查看"不许改时间、"采集"不许拿旧样本充新**。两个时间戳能对上，
就说明助手没有把两种意图搞混。

### 场景二：异常指令

| 输入 | 期望 |
|---|---|
| 越界 MAC（如 `AA:BB:CC:DD:EE:FF`，不在白名单） | `intent=reject`、`error.code=forbidden_device`、**零新增指令** |
| "帮我弄一下" | `intent=clarify`、`error.code=ambiguous_request`，给出二选一提示 |
| "采集"（光杆动作词） | `intent=clarify`，不替用户猜 |
| "停止采集" | `intent=reject`、`error.code=unsupported_action` |
| 旧数据查询但库里为空 | `ok=false`、`error.code=no_data`，`answer` 提示可重新采集 |
| 设备无响应（`wait_ms=0`） | `error.code=device_no_response`，带 request_id 可继续查 |
| 不配 API key / 模型超时 | `engine_used=rules`，功能照常，`trace.llm_error` 记录原因 |

以上每一条 `nl_demo.py` 都会打印 `[PASS]/[FAIL]`。

## 10. 验证命令

```bash
# 单元测试：前3周 130 项 + 第4周 26 项
cd server
python -X utf8 -m pytest -q

# 端到端验收（真实 HTTP，假设备扮演板端领取+回执，不碰真实库）
set TELEMETRY_DB=D:\esp32-s3-eye-telemetry\.scratch\wk4-demo.db
python -X utf8 -m uvicorn app:app --port 8012
# 另开一个终端：
python -X utf8 server/tools/nl_demo.py --url http://127.0.0.1:8012
```

`nl_demo.py` 用独立端口 + 独立库，跑完不影响正在用的 `data/telemetry.db`。
预期输出 `17 通过 / 0 失败`。

## 11. 已知边界

- **模型只翻译、不执行**，且模型输出按不可信输入处理；就算 `parse_llm` 被换坏，
  执行前还有白名单最后闸门。
- **规则词表需要人工维护**。新说法（如方言、缩写）可能要补 `CAPTURE_PATTERNS` /
  `QUERY_PATTERNS`；这是可离线、可确定性测试的代价，换来的是无 key 也能用。
- **多设备必须点名**。服务端见过多台且用户没说哪台时返回 `need_device`，
  绝不默认挑一台。
- **"查看上次"指连续流**。指令采集的样本默认不进连续流（第2周口径），
  要看某次指令采了什么，用 `request_id` 查。
- **`device_no_response` 不代表失败**。指令还在飞，拿 `action.request_id` 继续轮询即可。
- **默认等待窗口约 71 秒**（ttl+timeout+1s）；前端默认只等 12 秒，窗口小的时候
  更容易看到 `device_no_response`，这是如实报告而不是 bug。
- **LLM 通道依赖外部 key**。不配 key 时 `engine=auto` 全程走规则；
  模型故障不会让助手崩溃，只会降级并在 `trace` 里留痕。