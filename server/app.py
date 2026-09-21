"""ESP32-S3-EYE 遥测服务端。

时间戳原则：t_server_recv_ms 是唯一权威时间。设备时间只作为"参考值"存储，
并额外算出 device_clock_skew_ms（设备时间 - 服务端时间）让偏差本身可见。
"""

import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import buttons
import commands
import db

DB_PATH = os.environ.get(
    "TELEMETRY_DB", str(Path(__file__).parent / "data" / "telemetry.db")
)
STATIC_DIR = Path(__file__).parent / "static"
MAX_READINGS_PER_BATCH = 500

# 入库鉴权。不设则任何能访问到该端口的人都能以任意 MAC、任意时间戳注入数据，
# "数据来源可追溯"就无从谈起。本地开发可以不设，公网部署必须先设。
INGEST_TOKEN = os.environ.get("INGEST_TOKEN")

# 采集开关的鉴权，口径同 INGEST_TOKEN。这个开关比入库更该设：一旦被外人停掉，
# 板子照样在跑、只是不再上传，界面看上去和"设备失联"一模一样——受害者察觉不到。
CONTROL_TOKEN = os.environ.get("CONTROL_TOKEN")


@asynccontextmanager
async def lifespan(_app):
    conn = db.init_db(DB_PATH)
    commands.init_schema(conn)
    buttons.init_schema(conn)   # 第3周：按键事件表（幂等建表，旧库不受影响）
    conn.close()
    if not INGEST_TOKEN:
        print(
            "[warn] 未设置 INGEST_TOKEN：/api/v1/ingest 不校验来源，"
            "任何能访问该端口的人都能以任意 MAC 注入数据。公网部署前必须设置。"
        )
    if not CONTROL_TOKEN:
        print(
            "[warn] 未设置 CONTROL_TOKEN：任何人访问 /api/v1/control 都能远停采集，"
            "而界面上只会表现为设备失联。公网部署前必须设置。"
        )
    yield


app = FastAPI(title="ESP32-S3-EYE Telemetry", version="0.1.0", lifespan=lifespan)

# 允许跨源读取：网页用 file:// 直接打开时 Origin 是字符串 "null"，
# 不带这条中间件浏览器会拦掉所有响应，页面就永远是空的。
# 只用 allow_origins 不带 allow_credentials——不涉及 Cookie，开了反而会被浏览器拒绝。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def get_conn():
    conn = db.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


class Reading(BaseModel):
    seq: int = Field(ge=0)
    t_device_ms: int = Field(ge=0)
    ax: float | None = None
    ay: float | None = None
    az: float | None = None
    spl_db: float | None = None


class IngestBatch(BaseModel):
    device_mac: str
    boot_id: str
    # 第2周：非空表示这批样本是执行某条远程指令时采的。
    # 板端在指令回执之前先把样本走这条老路入库，于是"指令采集的数据"
    # 和"连续流数据"用同一套溯源口径（批次、seq、NTP 状态、服务端接收时刻）。
    request_id: str | None = None
    fw_version: str | None = None
    ntp_synced: bool = False
    ntp_sync_age_s: int | None = None
    t_device_ntp_ms: int | None = None
    dropped_since_last: int = Field(default=0, ge=0)
    readings: list[Reading] = Field(min_length=1, max_length=MAX_READINGS_PER_BATCH)


@app.get("/health")
def health(conn=Depends(get_conn)):
    conn.execute("SELECT 1").fetchone()
    return {"ok": True, "db": DB_PATH, "server_time_ms": int(time.time() * 1000)}


def require_ingest_token(request: Request):
    if not INGEST_TOKEN:
        return
    got = request.headers.get("x-ingest-token") or ""
    # 定时安全比较：逐字节比较会从耗时上泄漏密钥前缀
    if not secrets.compare_digest(got, INGEST_TOKEN):
        raise HTTPException(status_code=401, detail="X-Ingest-Token 缺失或不正确")


@app.post(
    "/api/v1/ingest", status_code=201, dependencies=[Depends(require_ingest_token)]
)
def ingest(batch: IngestBatch, request: Request, conn=Depends(get_conn)):
    seqs = [r.seq for r in batch.readings]
    # seq 范围由服务端从读数推导，不采信客户端上报的 seq_first/seq_last。
    # 这样即使传输途中批次被截断，缺口也会在下一次查询时真实暴露出来。
    rid = batch.request_id
    if rid is not None:
        rid = rid.strip()
        # 溯源信息必须是真实的：挂在一个不存在的指令上，比不挂更糟。
        if not commands.REQUEST_ID_RE.match(rid):
            raise HTTPException(status_code=400,
                                detail={"code": "bad_request_id",
                                        "message": "request_id 格式非法：%r" % rid})
        if commands.get_row(conn, rid) is None:
            raise HTTPException(status_code=400,
                                detail={"code": "unknown_request_id",
                                        "message": "request_id 不存在：%s" % rid})
    meta = {
        "request_id": rid,
        "device_mac": batch.device_mac.strip().upper(),
        "boot_id": batch.boot_id,
        "fw_version": batch.fw_version,
        "dropped_since_last": batch.dropped_since_last,
        "ntp_synced": batch.ntp_synced,
        "ntp_sync_age_s": batch.ntp_sync_age_s,
        "t_device_ntp_ms": batch.t_device_ntp_ms,
        "t_server_recv_ms": int(time.time() * 1000),
        "source_ip": request.client.host if request.client else None,
        "source_ua": request.headers.get("user-agent"),
        "seq_first": min(seqs),
        "seq_last": max(seqs),
        "readings": [r.model_dump() for r in batch.readings],
    }
    batch_id = db.insert_batch(conn, meta)
    if rid:
        commands.link_samples(conn, rid, batch_id, len(meta["readings"]))
    return {
        "ok": True,
        "batch_id": batch_id,
        "request_id": rid,
        "n_readings": len(meta["readings"]),
        "dropped_since_last": meta["dropped_since_last"],
        "t_server_recv_ms": meta["t_server_recv_ms"],
        "server_time_ms": int(time.time() * 1000),
    }


@app.get("/api/v1/readings")
def readings(
    since_ms: int | None = Query(default=None, description="按设备时间戳过滤（仅作参考）"),
    limit: int = Query(default=2000, ge=1, le=20000),
    device_mac: str | None = None,
    request_id: str | None = Query(default=None, description="只看某条指令采到的样本"),
    include_command_samples: bool = Query(
        default=False, description="把指令采集的样本一起返回（默认只返回连续流）"
    ),
    conn=Depends(get_conn),
):
    rows = db.query_readings(
        conn,
        since_ms=since_ms,
        limit=limit,
        device_mac=device_mac.strip().upper() if device_mac else None,
        request_id=request_id,
        include_command_samples=include_command_samples,
    )
    gaps = db.find_gaps(rows)
    last = rows[-1] if rows else None
    latest = None
    if last:
        latest = {
            "t_trust": db.trust_level(last["ntp_synced"], last["ntp_sync_age_s"]),
            "ntp_synced": bool(last["ntp_synced"]),
            "ntp_sync_age_s": last["ntp_sync_age_s"],
            "t_device_ntp_ms": last["t_device_ntp_ms"],
            "device_clock_skew_ms": (
                last["t_device_ntp_ms"] - last["t_server_recv_ms"]
                if last["t_device_ntp_ms"] is not None
                else None
            ),
        }
    # 默认过滤掉指令采集的样本，但必须把"过滤了多少"说出来：
    # 静默过滤和静默丢数据在界面上是同一件事。
    excluded = (
        0 if (request_id or include_command_samples)
        else db.count_command_samples(conn)
    )
    return {
        "ok": True,
        "server_time_ms": int(time.time() * 1000),
        "count": len(rows),
        "truncated": len(rows) == limit,
        "excluded_command_samples": excluded,
        # latest 只是"最新一条"的状态，counts 才是整个窗口的真实构成。
        # 每条样本自带 ntp_synced/ntp_sync_age_s，前端逐点打标时用行内字段。
        "trust": {"latest": latest, "counts": db.trust_counts(rows)},
        "gaps": gaps,
        "readings": rows,
    }


@app.get("/api/v1/status")
def status(conn=Depends(get_conn)):
    # 这里顺带结算指令超时。界面每 2 秒轮询一次 status，
    # 于是"到点该判超时"这件事最迟 2 秒后就会发生，不需要后台线程。
    return {"ok": True, **db.get_status(conn), "commands": commands.stats(conn)}


@app.get("/api/v1/batches")
def batches(
    limit: int = Query(default=50, ge=1, le=1000),
    conn=Depends(get_conn),
):
    rows = conn.execute(
        "SELECT * FROM batches ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return {"ok": True, "count": len(rows), "batches": [dict(r) for r in rows]}


class ControlRequest(BaseModel):
    collect: bool
    note: str | None = None


def require_control_token(request: Request):
    if not CONTROL_TOKEN:
        return
    got = request.headers.get("x-control-token") or ""
    if not secrets.compare_digest(got, CONTROL_TOKEN):
        raise HTTPException(status_code=401, detail="X-Control-Token 缺失或不正确")


@app.get("/api/v1/control")
def control_get(conn=Depends(get_conn)):
    return {"ok": True, **db.get_control(conn)}


@app.post(
    "/api/v1/control", dependencies=[Depends(require_control_token)]
)
def control_set(payload: ControlRequest, conn=Depends(get_conn)):
    return {"ok": True, **db.set_control(conn, payload.collect, payload.note)}


@app.get("/api/v1/control/state")
def control_state(conn=Depends(get_conn)):
    """给设备轮询用。刻意返回纯文本 "1"/"0"：板端解析 JSON 要多引一个库、
    多几百字节 RAM 和一个可能失败的解析分支，而这里只有一个布尔值。
    板端把"取不到"当成"照常采集"，所以这个接口刻意不设鉴权，免得密钥配错时
    板子静默停采——那种失败在界面上看不出来，恰恰是本项目最不想要的那种。
    """
    state = "1" if db.get_control(conn)["collect"] else "0"
    return Response(content=state, media_type="text/plain")


# ============================================================ 第2周：远程采集指令
#
# 两类调用方，鉴权口径不同，理由也不同：
#   网页下发/撤销 -> CONTROL_TOKEN。这是"能改设备行为"的操作，和采集开关同级。
#   设备领取/回执 -> INGEST_TOKEN。板子上已经配了这一把，不再多配一把：
#                    多一把就多一种配错的可能，而配错的后果是指令永远发不出去。
# 领取失败不会静默：指令会停在 pending，ttl 一到就变成 expired，界面上看得见。
# 这正是我们要的失败方式——对照第1周 control/state 刻意不鉴权，因为那边的失败
# 会表现为"板子静默停采"，是看不见的。


@app.exception_handler(commands.CommandError)
async def command_error_handler(_request: Request, exc: commands.CommandError):
    """指令层的业务错误统一出口。

    错误码是给程序看的（前端据此决定要不要提示重新输入令牌），
    message 是给人看的，两者都返回，避免前端去解析中文句子。
    """
    return JSONResponse(
        status_code=exc.http_status,
        content={"ok": False, "error": {"code": exc.code, "message": exc.message}},
    )


class CommandCreate(BaseModel):
    device_mac: str
    op: str
    params: dict | None = None
    # 幂等键：一次用户意图一个 token。双击、超时重试、断网重发都带同一个 token，
    # 服务端就只会产生一条指令。防抖不能只靠前端禁用按钮——
    # 前端状态会被刷新、被多标签页、被直接 curl 绕过。
    client_token: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=200)


class CommandResultIn(BaseModel):
    state: str  # running | done | failed
    boot_id: str
    device_mac: str | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    error_code: str | None = Field(default=None, max_length=64)
    error_message: str | None = Field(default=None, max_length=512)
    result: dict | None = None
    samples_batch_id: int | None = None
    n_samples: int | None = Field(default=None, ge=0)
    t_device_ms: int | None = None


@app.get("/api/v1/commands/ops")
def command_ops():
    """指令清单与参数范围。前端按这个渲染表单，不另抄一份范围——
    抄两份的话，改一边忘一边是迟早的事。"""
    return {
        "ok": True,
        "max_live_per_device": commands.MAX_LIVE_PER_DEVICE,
        "max_capture_duration_ms": commands.MAX_CAPTURE_DURATION_MS,
        "ops": commands.ops_catalog(),
    }


# 注册顺序要紧：字面量路径必须排在 /{request_id} 之前，
# 否则 "claim"、"ops" 会被当成 request_id 匹配掉。
@app.post("/api/v1/commands/claim", dependencies=[Depends(require_ingest_token)])
async def command_claim(request: Request, conn=Depends(get_conn)):
    """设备领取一条待执行指令。

    进出都是纯文本（`mac|boot_id|fw` -> `request_id|op|k=v;k=v|timeout_ms`），
    板端因此不需要 JSON 库。没有活干时返回字面量 "none"（200，不是 404：
    用 404 表达"没有"会逼板端多写一条错误分支）。
    """
    body = (await request.body()).decode("utf-8", "replace")
    mac, boot_id, fw = commands.decode_claim_request(body)
    row = commands.claim(conn, device_mac=mac, boot_id=boot_id, fw_version=fw)
    if row is None:
        return Response(content=commands.CLAIM_NONE, media_type="text/plain")
    return Response(content=commands.encode_claim(row), media_type="text/plain")


@app.post("/api/v1/commands", status_code=201,
          dependencies=[Depends(require_control_token)])
def command_create(payload: CommandCreate, request: Request, conn=Depends(get_conn)):
    row, deduped = commands.create(
        conn,
        device_mac=payload.device_mac,
        op=payload.op,
        params=payload.params,
        client_token=payload.client_token,
        note=payload.note,
        created_by=request.client.host if request.client else None,
    )
    body = commands.to_dict(row, commands.now_ms(),
                            commands.events_of(conn, row["request_id"]))
    # 幂等命中时返回 200 而不是 201：状态码本身就说明"这次点击没有产生新指令"，
    # 前端不必去比对时间戳来猜。
    return JSONResponse(
        status_code=200 if deduped else 201,
        content={"ok": True, "deduped": deduped, "command": body},
        headers={"X-Deduped": "1" if deduped else "0"},
    )


@app.get("/api/v1/commands")
def command_list(
    limit: int = Query(default=50, ge=1, le=500),
    state: str | None = Query(default=None, description="逗号分隔，如 pending,running"),
    device_mac: str | None = None,
    op: str | None = None,
    conn=Depends(get_conn),
):
    return {
        "ok": True,
        "server_time_ms": int(time.time() * 1000),
        "stats": commands.stats(conn),
        "commands": commands.list_commands(
            conn, limit=limit, state=state, device_mac=device_mac, op=op
        ),
    }


@app.get("/api/v1/commands/{request_id}")
def command_get(request_id: str, with_events: bool = True, conn=Depends(get_conn)):
    return {
        "ok": True,
        "server_time_ms": int(time.time() * 1000),
        "command": commands.get_command(conn, request_id, with_events=with_events),
    }


@app.post("/api/v1/commands/{request_id}/cancel",
          dependencies=[Depends(require_control_token)])
def command_cancel(request_id: str, conn=Depends(get_conn)):
    row = commands.cancel(conn, request_id)
    return {"ok": True, "command": commands.to_dict(row, commands.now_ms())}


@app.post("/api/v1/commands/{request_id}/result",
          dependencies=[Depends(require_ingest_token)])
def command_result(request_id: str, payload: CommandResultIn, conn=Depends(get_conn)):
    row = commands.apply_result(
        conn,
        request_id,
        state=payload.state,
        boot_id=payload.boot_id,
        device_mac=payload.device_mac,
        progress=payload.progress,
        error_code=payload.error_code,
        error_message=payload.error_message,
        result=payload.result,
        samples_batch_id=payload.samples_batch_id,
        n_samples=payload.n_samples,
        t_device_ms=payload.t_device_ms,
    )
    return {
        "ok": True,
        "command": commands.to_dict(row, commands.now_ms(),
                                    commands.events_of(conn, request_id)),
    }


# ============================================================ 第3周：按键闭环
#
# 场景：佩戴者按下板上按键 -> 板端立刻给本地物理反馈（不等网络）-> 事件上报
# 到这里 -> Web 实时看到 -> Web 点"回应/取消" -> 走第2周的指令通道下发 notify
# -> 设备领取并播放对应反馈。
#
# 鉴权口径与前两周一致：
#   板端上报按键 -> INGEST_TOKEN（和 ingest/claim/result 同一把，板子上只配一把）
#   Web 回应/取消 -> CONTROL_TOKEN（"能改设备行为"的操作，和下发指令同级）
#   读事件列表     -> 不设鉴权，口径同 /api/v1/readings（只读、不含密钥）


class ButtonPress(BaseModel):
    device_mac: str
    boot_id: str
    # press_seq 在"本次启动"内自增；幂等键 (mac, boot_id, press_seq) 保证
    # 板端重试不会把一次按键变成两次。
    press_seq: int = Field(ge=0)
    fw_version: str | None = Field(default=None, max_length=32)
    t_press_uptime_ms: int | None = Field(default=None, ge=0)
    t_device_ntp_ms: int | None = None
    ntp_synced: bool = False
    ntp_sync_age_s: int | None = Field(default=None, ge=0)
    queue_dropped: int = Field(default=0, ge=0)


@app.post("/api/v1/button", dependencies=[Depends(require_ingest_token)])
def button_press(payload: ButtonPress, conn=Depends(get_conn)):
    """板端上报一次按键。新事件 201；幂等命中（重试重发）200 + X-Deduped:1。

    状态码本身就说明"这次上传是不是第一次"，板端与排查的人都不必比对时间戳。
    """
    row, deduped = buttons.record_press(
        conn,
        device_mac=payload.device_mac,
        boot_id=payload.boot_id,
        press_seq=payload.press_seq,
        fw_version=payload.fw_version,
        t_press_uptime_ms=payload.t_press_uptime_ms,
        t_device_ntp_ms=payload.t_device_ntp_ms,
        ntp_synced=payload.ntp_synced,
        ntp_sync_age_s=payload.ntp_sync_age_s,
        queue_dropped=payload.queue_dropped,
    )
    body = buttons.to_dict(row, buttons.now_ms(),
                           buttons.command_of(conn, row["request_id"]))
    return JSONResponse(
        status_code=200 if deduped else 201,
        content={"ok": True, "deduped": deduped, "event": body},
        headers={"X-Deduped": "1" if deduped else "0"},
    )


@app.get("/api/v1/button/events")
def button_events(
    limit: int = Query(default=30, ge=1, le=200),
    device_mac: str | None = None,
    state: str | None = Query(default=None, description="逗号分隔：received,acked,cancelled"),
    conn=Depends(get_conn),
):
    """Web 端轮询用。顺带惰性结算指令超时（和 /api/v1/status 同一个机制），
    于是"notify 指令是否还活着"在这里看到的与指令面板永远一致。"""
    return {
        "ok": True,
        "server_time_ms": int(time.time() * 1000),
        "stats": buttons.stats(conn),
        "events": buttons.list_events(conn, limit=limit, device_mac=device_mac,
                                      state=state),
    }


class ButtonRespond(BaseModel):
    decision: str = Field(description="ack=回应, cancel=取消")
    client_token: str | None = Field(default=None, max_length=64)


@app.post("/api/v1/button/events/{event_id}/respond",
          dependencies=[Depends(require_control_token)])
def button_respond(event_id: int, payload: ButtonRespond, request: Request,
                   conn=Depends(get_conn)):
    """Web 对一次按键作出回应/取消。

    内部通过 buttons.respond() -> commands.create(op="notify") 生成指令，
    复用第2周整套领取/状态机/超时/审计；返回体里带上这条指令，
    前端可以直接把它的 request_id 和状态显示在事件行上。
    """
    row, cmd, deduped = buttons.respond(
        conn, event_id,
        decision=payload.decision,
        client_token=payload.client_token,
        actor=request.client.host if request.client else "web",
    )
    body = buttons.to_dict(row, buttons.now_ms(),
                           buttons.command_of(conn, cmd["request_id"]))
    return JSONResponse(
        status_code=200 if deduped else 201,
        content={"ok": True, "deduped": deduped, "event": body,
                 "command": commands.to_dict(cmd, commands.now_ms())},
        headers={"X-Deduped": "1" if deduped else "0"},
    )


# 界面挂在最后：Starlette 按注册顺序匹配，先注册的 /api 与 /health 不会被它抢走。
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
