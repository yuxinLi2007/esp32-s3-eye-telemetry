"""ESP32-S3-EYE 遥测服务端。

时间戳原则：t_server_recv_ms 是唯一权威时间。设备时间只作为"参考值"存储，
并额外算出 device_clock_skew_ms（设备时间 - 服务端时间）让偏差本身可见。
"""

import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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
    db.init_db(DB_PATH).close()
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
    meta = {
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
    return {
        "ok": True,
        "batch_id": batch_id,
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
    conn=Depends(get_conn),
):
    rows = db.query_readings(
        conn,
        since_ms=since_ms,
        limit=limit,
        device_mac=device_mac.strip().upper() if device_mac else None,
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
    return {
        "ok": True,
        "server_time_ms": int(time.time() * 1000),
        "count": len(rows),
        "truncated": len(rows) == limit,
        # latest 只是"最新一条"的状态，counts 才是整个窗口的真实构成。
        # 每条样本自带 ntp_synced/ntp_sync_age_s，前端逐点打标时用行内字段。
        "trust": {"latest": latest, "counts": db.trust_counts(rows)},
        "gaps": gaps,
        "readings": rows,
    }


@app.get("/api/v1/status")
def status(conn=Depends(get_conn)):
    return {"ok": True, **db.get_status(conn)}


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


# 界面挂在最后：Starlette 按注册顺序匹配，先注册的 /api 与 /health 不会被它抢走。
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
