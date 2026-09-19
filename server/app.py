"""ESP32-S3-EYE 遥测服务端。

时间戳原则：t_server_recv_ms 是唯一权威时间。设备时间只作为"参考值"存储，
并额外算出 device_clock_skew_ms（设备时间 - 服务端时间）让偏差本身可见。
"""

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import db

DB_PATH = os.environ.get(
    "TELEMETRY_DB", str(Path(__file__).parent / "data" / "telemetry.db")
)
STATIC_DIR = Path(__file__).parent / "static"
MAX_READINGS_PER_BATCH = 500

@asynccontextmanager
async def lifespan(_app):
    db.init_db(DB_PATH).close()
    yield


app = FastAPI(title="ESP32-S3-EYE Telemetry", version="0.1.0", lifespan=lifespan)


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


@app.post("/api/v1/ingest", status_code=201)
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


# 界面挂在最后：Starlette 按注册顺序匹配，先注册的 /api 与 /health 不会被它抢走。
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
