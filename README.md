# ESP32-S3-EYE 传感数据采集与 Web 展示

把一块 ESP32-S3-EYE 变成能自证可信的遥测节点：板端采集加速度与声压级，经 HTTP
上报到自建服务端，再由一个单文件网页实时展示。

项目不是为了"把数据画出来"，而是为了回答三个问题——**数据是谁给的、时间可不可信、
没有数据的时候是不是看得见**。所有设计取舍都回到这三点。

---

## 三条核心要求

| 要求 | 具体做法 |
|---|---|
| **数据来源可追溯** | 每次成功上传落一条 `batches` 记录：device_mac、boot_id、固件版本、来源 IP、User-Agent、服务端接收时刻。入库需带 `X-Ingest-Token`，否则任何能访问端口的人都能以任意 MAC 灌数据。 |
| **时间戳可信度明确** | 唯一权威时间是 `t_server_recv_ms`。设备时间只作参考，且必须由 NTP 是否同步过、同步了多久共同判定等级（`ntp_fresh` / `ntp_stale` / `ntp_expired` / `unsynced`），界面上逐条打标。**不落库成字段**，而是由原始事实派生——派生值只有一个来源，不会漂移。 |
| **无数据与上传失败必须在界面上被看见** | 静默失败不算数。设计上让"持续采样"本身成为心跳：界面安静即代表设备失联。丢样靠 `seq` 跳号判定并画成红色竖带；板上环形缓冲溢出丢弃的样本数随下次成功上传上报。 |

---

## 架构

```
  ┌──────────────────────────────────────────────────────────────────┐
  │                        板端  ESP32-S3-EYE                        │
  │                                                                  │
  │   QMA6100P (I2C 0x12) ─┐                                         │
  │                        ├──► 每 50ms 采一个 Sample                │
  │   I2S 麦克风 (16kHz)  ─┘         │                               │
  │                                  ▼                               │
  │                           环形缓冲 240 个（约 12 秒）             │
  │                                  │                               │
  │   每个样本随身携带：               │  每 2 秒取 ≤100 个            │
  │   · seq        本次启动内自增      ▼                              │
  │   · boot_id    每次启动随机    HTTP POST /api/v1/ingest          │
  │   · dropped_since_last          + X-Ingest-Token                 │
  │   · NTP 同步状态与年龄             │                              │
  └───────────────────────────────────┼──────────────────────────────┘
                                      │
                                      ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │                     服务端  FastAPI + SQLite (WAL)               │
  │                                                                  │
  │     t_server_recv_ms = 服务端收到时刻，唯一权威时间               │
  │                                                                  │
  │   ┌─────────────┐    ┌─────────────┐    ┌──────────────┐         │
  │   │  batches    │    │  readings   │    │ control_log  │         │
  │   │ 每次上传一条 │    │ 逐样本带 seq │    │ 开关变更历史  │         │
  │   │ 溯源记录     │    │             │    │              │         │
  │   └─────────────┘    └─────────────┘    └──────────────┘         │
  └───────────────────────────────────┬──────────────────────────────┘
                                      │  GET /api/v1/{status,readings,control}
                                      ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │              界面  index.html（单文件、无构建、无 CDN）           │
  │   ① 报警条（整页最显眼）  ② 指标卡  ③ 曲线：缺口画成红带          │
  │   ④ 开始/停止采集按钮                                           │
  └───────────────────────────────────┬──────────────────────────────┘
                                      │
                  每 2 秒轮询开关      │  GET /api/v1/control/state
                                      ▼
                               回到板端，决定是否继续采样
```

**为什么要两张表。** `batches` 是溯源单位（一次上传 = 一条记录），`readings` 是数据单位。
同 `boot_id` 内 `seq` 不连续就是板上确有丢样的**确凿证据**，不需要靠"时间间隔看起来
变大了"去猜。跨 `boot_id` 的跳变不算丢样，但会单独报出来——那是设备重启，同样意味着
数据不可信。

---

## 目录结构

```
esp32-s3-eye-telemetry/
├── firmware/                    # 板端（PlatformIO + Arduino）
│   ├── platformio.ini           # 板型覆盖：N8R8 需 8MB flash + Octal PSRAM
│   ├── include/
│   │   ├── config.h             # 硬件常量（全部由实测确定）
│   │   └── secrets.example.h    # 配置模板 → 复制成 secrets.h
│   └── src/
│       ├── main.cpp             # 采集循环、环形缓冲、上报、开关轮询
│       ├── sensors.cpp/.h       # I2C 加速度计 + 遗留 I2S 麦克风
│       └── secrets.h            # 凭据，.gitignore 排除
├── server/
│   ├── app.py                   # FastAPI 接口
│   ├── db.py                    # SQLite 存储层 + 可信度派生
│   ├── test_ingest.py           # 27 项测试
│   ├── static/index.html        # 整个界面（单文件）
│   ├── tools/fault_inject.py    # 故障注入：主动制造失败以验证"失败可见"
│   ├── tools/ui_check.mjs       # DOM 打桩跑界面脚本，断言报警判定
│   ├── .env.example             # 配置模板 → 复制成 .env
│   └── data/telemetry.db        # 运行期数据库，.gitignore 排除
├── docs/development-log.md      # 开发复盘：判断依据与被证伪的假设
└── start_server.bat             # 双击启动服务端
```

---

## 快速开始

### 1. 服务端

```bash
cd server
pip install -r requirements.txt
cp .env.example .env          # 填入 INGEST_TOKEN / CONTROL_TOKEN
python -m uvicorn app:app --host 0.0.0.0 --port 8000 --env-file .env
```

Windows 上直接双击仓库根目录的 `start_server.bat` 即可，它带好了 `--env-file`。

验证：`curl http://127.0.0.1:8000/health`，或浏览器打开 <http://localhost:8000/>。

> 未设置 `INGEST_TOKEN` / `CONTROL_TOKEN` 时服务端照常启动，但会在日志里打警告。
> 本地开发可以留空，**公网部署前必须设置**。

### 2. 板端

```bash
cd firmware
cp include/secrets.example.h include/secrets.h   # 填入 WiFi 与服务端地址
pio run -t upload
pio device monitor
```

`secrets.h` 里的 `SERVER_URL` 要填服务端所在机器的**局域网 IP**，不能是 `127.0.0.1`
（那指向板子自己）。

### 3. 界面

浏览器打开 <http://localhost:8000/> 即可。

也可以直接双击 `server/static/index.html`——页面会自动去找本机 `127.0.0.1:8000`
的服务端。**但服务端必须一直在运行**：数据存在它的 SQLite 里，光有一个 HTML 文件
变不出数据来。若服务端在别的机器上，用
`index.html?api=http://服务端地址:8000` 打开。

---

## 接口说明

`GET /health` — 存活检查。返回数据库路径与服务端时间。

### 写入

**`POST /api/v1/ingest`** → 201，需请求头 `X-Ingest-Token`

```jsonc
{
  "device_mac": "94:A9:90:1C:6F:D4",   // 服务端统一转大写
  "boot_id": "7A97EB4F",               // 每次启动随机，重启在服务端可见
  "fw_version": "0.1.0",
  "ntp_synced": true,
  "ntp_sync_age_s": 12,                // 未同步时为 null
  "t_device_ntp_ms": 1789800826093,    // 设备时间，仅作参考；未同步时 null
  "dropped_since_last": 0,             // 上次上传失败导致缓冲溢出丢弃的样本数
  "readings": [
    { "seq": 0, "t_device_ms": 1200, "ax": 0.01, "ay": -0.02, "az": 1.0, "spl_db": 42.3 }
  ]
}
```

`seq_first` / `seq_last` **由服务端从 readings 推导，不采信客户端上报**。这样即使
传输途中批次被截断，缺口也会在下次查询时真实暴露。单批上限 500 条。

### 读取（公开，无需令牌）

| 接口 | 说明 |
|---|---|
| `GET /api/v1/readings?limit=&since_ms=&device_mac=` | 返回 `{count, truncated, trust:{latest,counts}, gaps, readings}`。**`trust.counts` 才是整个窗口的真实构成**，只报最后一条会误导——一个窗口里可能混着同步过和没同步过的批次。 |
| `GET /api/v1/status` | 总批次数/样本数/累计丢弃/重启次数、最近 60 秒样本数、最新批次详情（含 `age_ms`、`device_clock_skew_ms`、`t_trust`）。 |
| `GET /api/v1/batches?limit=` | 原始溯源记录列表。 |
| `GET /api/v1/control` | 当前采集开关 + 变更历史。 |
| `GET /api/v1/control/state` | 设备轮询专用，`text/plain`，内容就是 `1` 或 `0`。 |

> `since_ms` 过滤的是 `t_device_ms`（设备本次启动内的毫秒数），**跨重启没有意义**。
> 界面刻意不使用它。

### 控制

**`POST /api/v1/control`**，需请求头 `X-Control-Token`

```jsonc
{ "collect": false, "note": "界面手动停止" }
```

状态没变则不写新记录，否则设备每 2 秒轮询一次也会把"变更历史"塞满。

> `GET /api/v1/control/state` **刻意不鉴权**。板端把"取不到"当成"照常采集"，配错
> 令牌会让它静默停采——那种失败在界面上看不出来，恰恰是本项目最不想要的。

---

## 时间戳可信度口径

以服务端接收时间为权威，设备时间只在 NTP 同步过且足够新鲜时才可参考：

| 等级 | 条件 | 含义 |
|---|---|---|
| `ntp_fresh` | 同步过且年龄 ≤ 300s | 设备时间可直接参考 |
| `ntp_stale` | 年龄 301–3600s | 参考价值下降 |
| `ntp_expired` | 年龄 > 3600s | 不可参考 |
| `unsynced` | 从未同步或年龄未知 | 不可参考，`device_clock_skew_ms` 为 null |

设备端 `NTP_RESYNC_MS` 取 4 分钟，必须明显小于 `NTP_FRESH_THRESHOLD_S`（300 秒），
否则同步年龄会一路上涨、所有样本被判成 `ntp_stale`，标签退化成常量。

判"同步成功"只认 SNTP 回调——`configTime()` 是异步的，之后读 `time()` 必然得到一个
"合理"的值（系统时钟本来就在走），会把"没收到任何应答"说成"刚刚同步过"。

---

## 验证

```bash
cd server
python -m pytest test_ingest.py -v        # 27 项
node tools/ui_check.mjs                   # 对着真实服务端跑界面脚本
node tools/ui_check.mjs --origin http://127.0.0.1:8001
```

"失败必须可见"是否定性要求——正常情况下永远看不到，所以验证不能靠"我觉得逻辑写对了"，
必须主动制造失败：

```bash
# 在隔离数据库上注入 4 种缺陷：seq 缺口、板上丢弃、NTP 未同步、彻底静默
python tools/fault_inject.py --url http://127.0.0.1:8001
```

`ui_check.mjs` 把 `index.html` 里的脚本抠出来，用最小 DOM 桩在 node 里跑，断言报警
等级与文案。这类断言让"该报警时必须报警"可以被确定性地回归，而不必真去拔电源。

---

## 已知限制

- **有效采样率约 16.6 Hz**，低于配置的 20 Hz。每轮循环有一次阻塞式 I2S 读取（约 16ms）。
  `t_device_ms` 如实记录，所以数据本身不误导，但采样间隔的抖动尚未处理。
- **断电时未上传的缓冲样本不计入任何计数器**，只能表现为 `boot_id` 变化加上服务端
  接收时刻的空白。
- **加速度计只做了单姿态标定**（静止时 `|a| = 1g`），无法把量程误差和零偏误差分开。
  换姿态若明显偏离 1，需要多姿态拟合。
- **`X-Ingest-Token` 目前是明文传输**，鉴权要真正有意义需配合 HTTPS。
- 部署到公网前必须设置 `INGEST_TOKEN` 与 `CONTROL_TOKEN`。

## 待办

- [ ] 部署到 VPS，配 HTTPS
- [ ] 处理采样率抖动
