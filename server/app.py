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

import assistant
import commands
import db
import snapshots
import task_context
import vision
import voice

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
    snapshots.ensure_schema(conn)
    vision.ensure_schema(conn)
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


@app.exception_handler(voice.VoiceError)
async def voice_error_handler(_request: Request, exc: voice.VoiceError):
    """语音链路（第5周）的错误出口，口径同上：code + message 一起返回。"""
    return JSONResponse(
        status_code=exc.http_status,
        content={"ok": False, "error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(snapshots.SnapshotError)
async def snapshot_error_handler(_request: Request, exc: snapshots.SnapshotError):
    """图像通道（第7周）的错误出口，shape 与指令层一致：code + message。"""
    return JSONResponse(
        status_code=exc.http_status,
        content={"ok": False, "error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(vision.VisionError)
async def vision_error_handler(_request: Request, exc: vision.VisionError):
    """视觉链路（第8周）的错误出口，shape 同上：code + message。"""
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


# ============================================================ 第7周：按需环境快照
#
# 图像通道与指令通道同构：
#   下发（CONTROL_TOKEN）-> 设备领取（INGEST_TOKEN）-> 传图 POST /commands/{rid}/image
#   -> done 回执（INGEST_TOKEN）。done 回执前必须先传图（commands.apply_result 强制），
#   图像记录（snapshots 表 + 磁盘文件）带服务端接收时刻（权威）与设备声称采集时刻（参考）。
#   终止指令一律拒收图像：迟到/重放的旧图绝不可能挂到新请求上、
#   也不可能让一次已撤销/失败变成"成功"。


def _hint(v):
    """把请求头里的整数 hint 转成 int/None，坏值按没有处理（不因头写错 500）。"""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@app.post("/api/v1/commands/{request_id}/image",
          dependencies=[Depends(require_ingest_token)])
async def command_image_upload(request_id: str, request: Request,
                               conn=Depends(get_conn)):
    """设备上传一张图（字节流）。任何失败都带明确错误码（改布局见 SnapshotError）。
    幂等：同一 request_id 重复上传拿到同一条记录，不产生第二张图。"""
    data = await request.body()
    snap = snapshots.store(
        conn, request_id, data,
        device_mac=request.headers.get("x-device-mac"),
        boot_id=request.headers.get("x-boot-id"),
        source=request.headers.get("x-source") or "camera",
        t_captured_ms=_hint(request.headers.get("x-captured-at-ms")),
        width=_hint(request.headers.get("x-width")),
        height=_hint(request.headers.get("x-height")),
        content_type=request.headers.get("content-type") or "image/jpeg",
    )
    return {"ok": True, "snapshot": snapshots._meta(snap),
            "server_time_ms": int(time.time() * 1000)}


@app.get("/api/v1/snapshots")
def snapshots_list(
    limit: int = Query(default=50, ge=1, le=200),
    request_id: str | None = Query(default=None, description="只看某条图像指令的图"),
    conn=Depends(get_conn),
):
    rows = snapshots.list_snapshots(conn, limit=limit, request_id=request_id)
    return {"ok": True, "count": len(rows), "snapshots": [snapshots._meta(r) for r in rows]}


@app.get("/api/v1/snapshots/{snapshot_id}/image")
def snapshot_image(snapshot_id: int, conn=Depends(get_conn)):
    """图像本体字节流。文件已按"最少保留"清理时如实 404（元数据不见了就看图不见）。"""
    row = snapshots.get(conn, snapshot_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail={"code": "snapshot_not_found",
                                    "message": "图像记录不存在（可能已被最少保留策略清理）"})
    p = snapshots.path_for(row)
    if not p.is_file():
        raise HTTPException(status_code=404,
                            detail={"code": "image_file_missing",
                                    "message": "图像文件已缺失（最少保留已清理或磁盘异常）"})
    return Response(content=p.read_bytes(),
                    media_type=row["content_type"] or "image/jpeg",
                    headers={"X-Snapshot-Id": str(row["id"]),
                             "X-Source": row["source"] or "camera",
                             "X-Captured-At-Ms": str(row["t_server_recv_ms"])})


# ============================================================ 第8周：视觉推理与反馈
#
# 模型返回"标签 + 分数 + 拒识 + 模型版本"，系统把它们变成可理解反馈：
#   - analyze     —— 对一张已采集图像做推理（结构化解析，幂等；未配置模型走"回放"标注）；
#   - corrections —— 人工纠正入口（纠正值覆盖模型推断展示，供第9周"误判可纠正"复用）；
#   - recapture   —— 重新采集（复用 image 指令通道，同设备补拍一张）。
# 鉴权口径：写状态/改设备行为的操作（analyze/corrections/recapture/ask）与
# cancel/image 同级，要求 CONTROL_TOKEN。只读查询匿名可用，与 query 同级。
# 第9周主动询问挂在 analyze 事件上：画面不可用 → 自动开一条"要重拍吗"的询问，
# 用户可 confirm（重拍）/ ignore（忽略 + 冷却）；GET /asks 供前端横幅轮询。


class AnalyzeIn(BaseModel):
    task: str = Field(default="usability", max_length=32)
    force: bool = False


@app.post("/api/v1/snapshots/{snapshot_id}/analyze",
          dependencies=[Depends(require_control_token)])
def snapshot_analyze(snapshot_id: int, payload: AnalyzeIn, conn=Depends(get_conn)):
    snap = snapshots.get(conn, snapshot_id)
    if snap is None:
        raise HTTPException(status_code=404,
                            detail={"code": "snapshot_not_found",
                                    "message": "图像记录不存在（可能已被最少保留策略清理）"})
    inf = vision.analyze_snapshot(conn, snap, task=payload.task or "usability",
                                  force=payload.force)
    out = {"ok": True, "snapshot_id": snapshot_id, "inference": inf}
    # 第9周事件触发：画面不可用 → 主动询问"要重拍吗"（资格不满足则给原因，不打扰）
    ask, ask_reason = vision.open_ask(conn, snap)
    if ask is not None:
        out["ask"] = vision._view_ask(ask, snap)
    else:
        out["ask_note"] = ask_reason
    return out


class AskIn(BaseModel):
    decision: str = Field(pattern="^(confirm|ignore)$")


@app.post("/api/v1/snapshots/{snapshot_id}/ask",
          dependencies=[Depends(require_control_token)])
def snapshot_ask(snapshot_id: int, payload: AskIn, conn=Depends(get_conn)):
    """用户对"要重拍吗"表态：confirm → 立即下发重拍指令；ignore → 冷却期不打扰。"""
    snap = snapshots.get(conn, snapshot_id)
    if snap is None:
        raise HTTPException(status_code=404,
                            detail={"code": "snapshot_not_found",
                                    "message": "图像记录不存在"})
    ask = vision.decide_ask(conn, snap, payload.decision)
    out = {"ok": True, "ask": vision._view_ask(ask, snap)}
    if payload.decision == "confirm":
        if not snap["device_mac"]:
            raise HTTPException(status_code=400,
                                detail={"code": "no_device",
                                        "message": "该图像没有关联设备，无法重拍"})
        token = "ask-" + secrets.token_hex(8)
        row, deduped = commands.create(conn, device_mac=snap["device_mac"],
                                       op="image", params={}, client_token=token,
                                       note="ask confirm recapture of snapshot %d"
                                            % snapshot_id,
                                       created_by="ask")
        out["recapture"] = commands.to_dict(
            row, commands.now_ms(), commands.events_of(conn, row["request_id"]))
        out["deduped"] = deduped
    return out


@app.get("/api/v1/asks")
def asks_list(
    limit: int = Query(default=20, ge=1, le=100),
    pending: bool = Query(default=True, description="只列出未表态的询问"),
    conn=Depends(get_conn),
):
    """待用户表态的主动询问（第9周，匿名只读；前端横幅按它轮询）。"""
    rows = vision.pending_asks(conn, limit=limit) if pending else []
    return {"ok": True, "count": len(rows), "asks": rows}


class CorrectionIn(BaseModel):
    kind: str = Field(default="label", max_length=16)
    value: str = Field(min_length=1, max_length=32)
    note: str | None = Field(default=None, max_length=200)


@app.post("/api/v1/snapshots/{snapshot_id}/corrections",
          dependencies=[Depends(require_control_token)])
def snapshot_correct(snapshot_id: int, payload: CorrectionIn, conn=Depends(get_conn)):
    snap = snapshots.get(conn, snapshot_id)
    if snap is None:
        raise HTTPException(status_code=404,
                            detail={"code": "snapshot_not_found",
                                    "message": "图像记录不存在"})
    row = vision.add_correction(conn, snap, kind=payload.kind,
                                value=payload.value, note=payload.note)
    out = dict(row)
    out["effective"] = vision.effective_label(conn, snapshot_id)[0]
    return {"ok": True, "correction": out}


@app.post("/api/v1/snapshots/{snapshot_id}/recapture",
          dependencies=[Depends(require_control_token)])
def snapshot_recapture(snapshot_id: int, request: Request, conn=Depends(get_conn)):
    """重新采集：同样的设备、同样的 image 指令通道，再拍一张（新 request_id）。"""
    snap = snapshots.get(conn, snapshot_id)
    if snap is None:
        raise HTTPException(status_code=404,
                            detail={"code": "snapshot_not_found",
                                    "message": "图像记录不存在"})
    mac = snap["device_mac"]
    if not mac:
        raise HTTPException(status_code=400,
                            detail={"code": "no_device",
                                    "message": "该图像没有关联设备，无法重拍"})
    token = "ui-" + secrets.token_hex(8)
    row, deduped = commands.create(conn, device_mac=mac, op="image", params={},
                                   client_token=token,
                                   note="recapture of snapshot %d" % snapshot_id,
                                   created_by="ui")
    body = commands.to_dict(row, commands.now_ms(),
                            commands.events_of(conn, row["request_id"]))
    return JSONResponse(
        status_code=200 if deduped else 201,
        content={"ok": True, "deduped": deduped, "command": body},
    )


@app.get("/api/v1/snapshots/{snapshot_id}")
def snapshot_detail(snapshot_id: int,
                    task: str = Query(default="usability", max_length=32),
                    conn=Depends(get_conn)):
    """单张图像详情：元数据 + 推理结果 + 人工纠正（第8周，匿名只读）。"""
    snap = snapshots.get(conn, snapshot_id)
    if snap is None:
        raise HTTPException(status_code=404,
                            detail={"code": "snapshot_not_found",
                                    "message": "图像记录不存在（可能已被最少保留策略清理）"})
    inf = vision.get(conn, snapshot_id, task)
    ask = vision.get_ask(conn, snapshot_id)
    return {
        "ok": True,
        "snapshot": snapshots._meta(snap),
        "inference": vision._view(inf) if inf else None,
        "corrections": [dict(c) for c in vision.corrections_of(conn, snapshot_id)],
        "effective": vision.effective_label(conn, snapshot_id),
        "ask": vision._view_ask(ask, snap) if ask else None,
    }


@app.get("/api/v1/inferences")
def inferences_list(
    limit: int = Query(default=50, ge=1, le=200),
    request_id: str | None = Query(default=None, description="只看某条图像指令的推理"),
    conn=Depends(get_conn),
):
    """推理审计流（匿名只读）。即使图像本体被最少保留清理，
    请求(request_id)-结果(inference)-反馈(corrections)的对应关系仍可反查。"""
    rows = vision.list_inferences(conn, limit=limit, request_id=request_id)
    return {"ok": True, "count": len(rows),
            "inferences": [vision._view(r) for r in rows]}


# ============================================================ 第4周：自然语言入口
#
# 查询与请求采集分离：query 只读已有记录；collect 走 commands.create（改设备行为），
# 因此 collect 才需要 CONTROL_TOKEN，query 不需要。判断在服务端做，
# 前端没有任何"猜意图"的代码，也就没有"前端猜错了"的可能。


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    # 第6周：会话上下文 id（页面自持一个 UUID hex）。缺省 = 无上下文（等于第 4/5 周行为）。
    context_id: str | None = Field(default=None, max_length=40)


@app.post("/api/v1/assistant/chat")
def assistant_chat(payload: ChatIn, request: Request, conn=Depends(get_conn)):
    if payload.context_id and not task_context.CONTEXT_ID_RE.match(payload.context_id):
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_context_id",
                    "message": "context_id 格式非法"},
        )
    result = assistant.chat(conn, payload.text, context_id=payload.context_id)
    # collect / image / recapture / ask_ignore（改设备行为或改询问状态）与
    # cancel（改指令状态）都是会改变状态的，同级别鉴权。
    if result["intent"]["action"] in ("collect", "cancel", "image",
                                      "recapture", "ask_ignore"):
        require_control_token(request)
    return {
        "ok": True,
        "server_time_ms": int(time.time() * 1000),
        "intent": result["intent"],
        "answer": result["answer"],
        "command": result["command"],
        "error_code": result.get("error_code"),
        "context": result.get("context"),
    }


# ============================================================ 第5周：语音链路
#
# 转写 / 合成都是"只读"操作（不改变设备行为），鉴权口径与第 4 周"查询"一致（匿名）：
# 语音服务的密钥只存在服务端环境变量（VOICE_*），前端和设备的请求里从不携带密钥。
# 未配置 / 超时 / 无语音 / 服务报错统一走 voice.VoiceError 出口（带错误码），
# 不做任何"假装成功"。


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


@app.get("/api/v1/voice/info")
def voice_info():
    """运行位置与配置状态：界面据此如实标注音频来源（电脑音频 vs 设备音频）。"""
    return voice.info()


@app.post("/api/v1/voice/transcribe")
async def voice_transcribe(request: Request):
    """音频 -> 文本（ASR）。失败时返回 4xx/5xx + error.code，绝无假文本。"""
    audio = await request.body()
    text = voice.transcribe(audio, request.headers.get("content-type"))
    return {"ok": True, "text": text}


@app.post("/api/v1/voice/speak")
def voice_speak(payload: SpeakIn):
    """文本 -> 合成音频字节流（TTS）。合成失败时返回 4xx/5xx + error.code。"""
    data, media = voice.speak(payload.text)
    return Response(content=data, media_type=media)


@app.get("/api/v1/voice/context")
def voice_context(context_id: str, conn=Depends(get_conn)):
    """第6周：会话上下文视图（澄清挂起项 / 取消标记 / request_id 关联日志 / 计时）。

    只暴露"这个会话自己产生"的状态（context_id 是不可猜的随机串），
    只读不改；顺手把挂着的采集指令的终态时序账结掉（与 status 相同的懒结算思路）。
    """
    ctx = task_context.get(context_id)
    if ctx is None:
        raise HTTPException(status_code=404,
                            detail={"code": "context_not_found",
                                    "message": "会话不存在或已过期"})
    act = ctx.get("active")
    if act:
        try:
            c = commands.get_command(conn, act["request_id"], with_events=False)
            if c["state"] in commands.TERMINAL:
                t_terminal = c.get("t_finished_ms") or commands.now_ms()
                task_context.observe_terminal(context_id, act["request_id"],
                                              t_terminal)
        except commands.CommandError:
            pass
    return {"ok": True, "context": task_context.public_view(context_id)}


# 界面挂在最后：Starlette 按注册顺序匹配，先注册的 /api 与 /health 不会被它抢走。
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
