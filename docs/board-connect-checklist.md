# 插上板子网页没数据 —— 排查清单

> **这份文档只解决一个问题**：板子插上电了、网页 <http://localhost:8000/> 打开是空的。
> 它是 2026-09-20 真机联调那次「查了一个多小时」的固化版。下次照做，几分钟内定位。

---

## 一、一分钟版（每次上板都这么做）

```bat
:: 1. 板子 USB 插上，手机热点打开（热点名/密码要和 secrets.h 里一致）
:: 2. 双击仓库根目录的 start_server.bat，窗口开着别关
:: 3. 跑体检
D:\anaconda3\python.exe server\tools\doctor.py
:: 4. 有 FAIL 就按它打印的 -> 提示修；防火墙那两项 FAIL 就跑：
powershell -ExecutionPolicy Bypass -File server\tools\fix_firewall.ps1
:: 5. 浏览器 Ctrl+F5 强制刷新 http://localhost:8000/
```

`doctor.py` 是**只读诊断**，不会改你系统任何东西；`fix_firewall.ps1` 会改防火墙，会弹 UAC。

---

## 二、这次（2026-09-20）真正的原因

板子通电、WiFi 连上了、服务端也在跑，网页就是空的。根因是**防火墙**：

| 现象 | 根因 | 修法 |
|---|---|---|
| 串口刷 `[uplink] HTTP -1`，服务端日志一片空白 | Windows 防火墙里有一条**拦住全部入站**的 Block 规则（`codex_sandbox_offline_block_inbound`，沙箱环境加的）。**Block 优先级高于 Allow**，所以「放行了 8000」完全不起作用 | 禁用那条 Block + 放行 8000：<br>`powershell -ExecutionPolicy Bypass -File server\tools\fix_firewall.ps1` |

一句话记住：**板子连不上服务端时，先怀疑防火墙有没有「一刀切拦入站」，而不是先怀疑代码。**
判断特征——服务端日志里连一条请求都没有，说明包根本没到进程，不是应用层的问题。

同一次联调还顺手修了 3 个固件 bug（都已烧录进 0.2.0，见 `docs/week2-commands.md`）：
`split_claim` 拒绝空参数段、回执 JSON 开头多逗号、终态回执失败后重跑整条采集。
它们的表现是「指令下发没反应 / 一直 running」，**不是**「网页无数据」，别混在一起查。

---

## 三、数据流向：卡在哪一段，看什么

```
①板子采集 → ②WiFi 发出 → ③本机 IP:8000 → ④写 SQLite → ⑤网页读接口
```

| 段 | 卡住的表现 | 第一时间看 |
|---|---|---|
| ① 采集 | 串口没有 `===== 开始采集 =====` | 串口日志 `pio device monitor -p COM5 -b 115200` |
| ② WiFi | 串口反复 `WiFi 连接失败` | 热点是不是 2.4 GHz、名字密码对不对、`secrets.h` 改完有没有重新烧录 |
| ③ 到本机 | `[uplink] HTTP -1`（连不上）/ `401`（令牌错） | 防火墙、`SERVER_URL` 是不是本机当前局域网 IP、服务端是不是 `--host 0.0.0.0` |
| ④ 写库 | 板子报 422 | 服务端日志里的校验错误详情 |
| ⑤ 网页 | 命令行有数据、网页空 | 服务端窗口是不是关了；浏览器缓存（Ctrl+F5）；是不是用 `index.html` 双击打开却连错了 api 地址 |

**「③ 到本机」是 90% 的问题所在**，也是 doctor.py 检查最密的一段。

---

## 四、完整七步（第一次接板子，或换了网络之后）

```bat
:: 第 1 步：确认服务端配置里有令牌
type server\.env
::   期望看到 INGEST_TOKEN=xxxx。没有就： copy server\.env.example server\.env 再生成一个
D:\anaconda3\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"

:: 第 2 步：查本机当前局域网 IP（热点重连后会变！）
ipconfig
::   找 172.20.10.x（手机热点）或 192.168.x.x（家里路由），记下来

:: 第 3 步：把 IP 和令牌写进固件配置
notepad firmware\include\secrets.h
::   #define WIFI_SSID     "你的热点名"
::   #define WIFI_PASSWORD "热点密码"
::   #define SERVER_URL    "http://第2步查到的IP:8000"     ← 绝对不能写 127.0.0.1
::   #define INGEST_TOKEN  "和 server\.env 里一模一样"
::   存盘时编码选 UTF-8（热点名有中文时必须）

:: 第 4 步：烧录（COM 号用 doctor.py 报的那个，不一定是 COM5）
cd firmware
pio run -t upload --upload-port COM5
cd ..

:: 第 5 步：启动服务端，窗口保持打开
start_server.bat
::   等价于： python -m uvicorn app:app --host 0.0.0.0 --port 8000 --env-file .env
::   关键是 --host 0.0.0.0；只绑 127.0.0.1 的话板子永远连不进来

:: 第 6 步：体检，按提示修到全 PASS
D:\anaconda3\python.exe server\tools\doctor.py

:: 第 7 步：看串口确认在上报，再开网页
pio device monitor -p COM5 -b 115200     :: 期望每 2 秒一条 [uplink] OK
:: 浏览器 Ctrl+F5 打开 http://localhost:8000/
```

> 第 3 步改完 `secrets.h` **必须重新烧录**才生效。这是最容易忘的一步：
> 改了文件、重启了服务端、网页还是空的——因为板子里跑的还是旧地址。

---

## 五、症状速查表

| 看到什么 | 意思 | 怎么办 |
|---|---|---|
| 串口 `[uplink] HTTP -1` | 包发出去了，连不上服务端 | 防火墙（第二节）、`SERVER_URL` 的 IP、服务端有没有起 |
| 服务端日志**完全**没有请求 | 包没到进程 | 几乎一定是防火墙，跑 `fix_firewall.ps1` |
| 串口 `[uplink] 401` | 令牌不一致 | `secrets.h` 的 `INGEST_TOKEN` 抄成 `server\.env` 里的，重新烧录 |
| 串口 `[uplink] 422` | 报文格式不对 | 看服务端日志的 detail；确认烧的是当前代码 |
| 串口一片空白 | 没通电 / 没驱动 / COM 号不对 | 设备管理器里看实际是 COM 几；`platformio.ini` 需要 `ARDUINO_USB_CDC_ON_BOOT=1` |
| 串口反复 `WiFi 连接失败` | 连不上热点 | 必须 2.4 GHz；名字密码含中文时 `secrets.h` 要存 UTF-8；手机热点息屏会断 |
| doctor 全 PASS 但网页空 | 前端侧问题 | Ctrl+F5；确认打开的是 `http://localhost:8000/` 而不是别处的 `index.html` |
| 网页有数据但不再更新 | 板子掉线或服务端停了 | `doctor.py` 会报「最新批次已是 N 分钟前」；看服务端窗口是否还开着 |
| 指令一直 `running` 不动 | 板子没领到 / 没回终态 | 见 `docs/week2-commands.md`；确认 `CONTROL_TOKEN` 与固件一致 |

---

## 六、doctor.py 到底查了什么（11 项）

按数据流向排的，**靠前的项会连累靠后的项**，修的时候从上往下修：

| # | 检查项 | 挂了意味着 |
|---|---|---|
| 1 | `secrets.h` 存在 | 板子不知道该连谁，配置模板没复制 |
| 2 | `WIFI_SSID` 已填写 | 连不上网 |
| 3 | `SERVER_URL` 不是回环地址 | 填了 `127.0.0.1`＝让板子连它自己 |
| 4 | `SERVER_URL` 指向本机网卡 | IP 变了（热点重连最常见） |
| 5 | 入库令牌两侧一致 | 401 |
| 6 | 本机局域网 IP | 查不到就是没联网，第 4 项也没法判 |
| 7 | 服务端在监听 8000（且是 `0.0.0.0`） | 服务端没起，或只绑了回环 |
| 8 | 无全局入站拦截规则 | **本次的元凶**，Block 压过 Allow |
| 9 | 端口 8000 入站已放行 | 板子被墙在门外 |
| 10 | 检测到串口 | 板子没插好 / 没驱动 |
| 11 | 设备数据正在入库（+固件是否最新） | 最终裁决：库里近 60 秒有没有新数据 |

前 10 项全 PASS 而第 11 项 FAIL，说明问题在**板子侧**，去看串口日志。
11 项全 PASS 而网页还是空的，说明问题在**浏览器侧**，Ctrl+F5 或换 `http://localhost:8000/`。

---

## 七、换网络（换热点、换 WiFi、热点重连）之后必须做的三件事

手机热点每次重连，本机 IP 都可能变。IP 一变，板子就往一个不存在的地址发数据：

1. `ipconfig` 查新的本机 IP
2. 改 `firmware\include\secrets.h` 里的 `SERVER_URL`
3. `cd firmware && pio run -t upload --upload-port COM5` 重新烧录

然后 `python server\tools\doctor.py` 复查。doctor.py 的第 4 项就是专门抓这个的：
它会把 `secrets.h` 里的 IP 和 `ipconfig` 查到的实际 IP 做比对，不一致直接 FAIL 并给出改法。

---

## 八、常用命令备忘

```bat
:: 体检（最常用）
D:\anaconda3\python.exe server\tools\doctor.py
D:\anaconda3\python.exe server\tools\doctor.py --port 8000

:: 修防火墙（弹 UAC 点“是”）
powershell -ExecutionPolicy Bypass -File server\tools\fix_firewall.ps1
powershell -ExecutionPolicy Bypass -File server\tools\fix_firewall.ps1 -Port 8000

:: 看串口日志
cd firmware && pio device monitor -p COM5 -b 115200

:: 查库里最新一批数据（不开网页也能确认有没有数据）
D:\anaconda3\python.exe -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/status').read().decode())"

:: 手动放行端口（管理员 PowerShell）
netsh advfirewall firewall add rule name="Telemetry 8000" dir=in action=allow protocol=TCP localport=8000 profile=any
```

**还原现场**：`fix_firewall.ps1` 只禁用不删除全局拦截规则。需要恢复时（管理员 PowerShell）：

```powershell
Set-NetFirewallRule -Name "codex_sandbox_offline_block_inbound" -Enabled True
```

---

## 相关文档

- [`docs/week2-commands.md`](week2-commands.md) —— 第2周远程指令通道设计与验证
- [`docs/development-log.md`](development-log.md) —— 开发复盘，含本次真机联调全过程
- [`README.md`](../README.md) —— 系统架构、接口文档、已知限制
