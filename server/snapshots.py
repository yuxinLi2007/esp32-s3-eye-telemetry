"""第7周：按需环境快照（on-demand image capture）的存储与最少保留。

三句话讲清楚本周口径（对着课程第 7 周的验收点）：

- 一张图 = 一条 snapshots 元数据记录，图像本体是磁盘文件（不塞 SQLite BLOB）。
  元数据把三样东西绑在一起：发起请求（request_id）、图像记录、采集时间。
- 权威时间 = t_server_recv_ms（服务端收到图像字节的时刻）。设备声称的
  t_captured_ms 只作参考并列存——与时间戳可信度同一哲学：派生值只从
  一个来源长出来，不让设备时间成为第二份权威。
- source = 'camera'（实拍）| 'replay'（获授权历史图像，必须标注"回放"，
  不得冒充实拍）。上次成功图像不得被标为本次采集成功：一次请求一张图、
  一个独立 request_id，done 回执强制"先传图"，迟到的旧图被终态守卫拒收。

最少保留（brief 的"设置最少保留"）：
  数量上限 SNAPSHOT_KEEP_MAX（默认 20）+ 时间上限 SNAPSHOT_KEEP_MS
  （默认 24 小时），每次成功入库后惰性清理超出的文件与记录。
"""

import hashlib
import os
import re
import time
from pathlib import Path

# 图像本体目录（默认 server/data/snapshots）
SNAPSHOT_DIR = os.environ.get(
    "SNAPSHOT_DIR",
    str(Path(__file__).parent / "data" / "snapshots"),
)
# 最少保留：数量 / 时间上限。模块级常量，测试用 monkeypatch 覆盖。
SNAPSHOT_KEEP_MAX = int(os.environ.get("SNAPSHOT_KEEP_MAX", "20") or 20)
SNAPSHOT_KEEP_MS = int(os.environ.get("SNAPSHOT_KEEP_MS", "86400000") or 86400000)

# 单张图的上限：按需快照一张即可，"环境画面"不需要高清原图。
MAX_IMAGE_BYTES = 2 * 1024 * 1024

_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class SnapshotError(Exception):
    """图像通道的业务错误出口，shape 与 commands.CommandError 一致。"""

    def __init__(self, message, code="bad_request", http_status=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status


def now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------- 表结构
SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id       TEXT NOT NULL,
    device_mac       TEXT NOT NULL,
    boot_id          TEXT,
    source           TEXT NOT NULL DEFAULT 'camera',
    t_captured_ms    INTEGER,
    t_server_recv_ms INTEGER NOT NULL,
    file_name        TEXT NOT NULL,
    content_type     TEXT,
    sha256           TEXT,
    n_bytes          INTEGER,
    width            INTEGER,
    height           INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_snapshots_request ON snapshots (request_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_recv ON snapshots (t_server_recv_ms);
"""


def ensure_schema(conn):
    """建表（幂等）。应用启动时与 store 前都会调，旧库升级不丢数据。"""
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------- 读写
def store(conn, request_id, data, *, device_mac=None, boot_id=None,
          source="camera", t_captured_ms=None, content_type="image/jpeg",
          width=None, height=None, now=None):
    """入库一张图（字节 → 文件 + 元数据）。

    铁律（对应当堂验证）：
      - 只有 op=image 且还没进终态的指令能收图，否则 409——迟到的旧图绝无可能
        挂到新请求上、更不可能让一次撤销/失败变成"成功"。
      - 同 request_id 只收一次：设备断网重发拿到的是同一条记录（幂等），
        不会出现"两张图抢一个请求"。
    返回入库后的元数据字典（含 id）。
    """
    ensure_schema(conn)
    now = now_ms() if now is None else now
    rid = (request_id or "").strip()
    row = conn.execute(
        "SELECT op, state FROM commands WHERE request_id = ?", (rid,)
    ).fetchone()
    if row is None:
        raise SnapshotError("指令不存在：%s" % rid, "not_found", 404)
    if row["op"] != "image":
        raise SnapshotError(
            "指令 %s 不是图像指令（op=%s），拒绝收图" % (rid, row["op"]),
            "not_image_op", 409,
        )
    if row["state"] in ("done", "failed", "expired", "timeout", "cancelled"):
        raise SnapshotError(
            "指令 %s 已是终态（%s），迟到/重放的图像一律拒收"
            % (rid, row["state"]),
            "already_terminal", 409,
        )
    if not data:
        raise SnapshotError("图像内容为空", "empty_image", 400)
    if len(data) > MAX_IMAGE_BYTES:
        raise SnapshotError(
            "图像过大：%d 字节，上限 %d" % (len(data), MAX_IMAGE_BYTES),
            "image_too_large", 413,
        )
    source = source or "camera"
    if not _SOURCE_RE.match(str(source)):
        raise SnapshotError("source 非法：%r（应如 camera / replay）" % source,
                            "bad_source")
    if source == "replay" and not device_mac:
        raise SnapshotError("回放图像必须带设备 MAC（X-Device-Mac）", "bad_source")

    # 幂等：同 request_id 已有记录 → 原样返回（设备重试不产生第二张图）
    old = conn.execute(
        "SELECT * FROM snapshots WHERE request_id = ?", (rid,)
    ).fetchone()
    if old is not None:
        return dict(old)

    ext = {"image/jpeg": "jpg", "image/png": "png",
           "image/webp": "webp"}.get((content_type or "").split(";")[0].strip(),
                                     "bin")
    file_name = "%s.%s" % (rid, ext)       # rid 只含 [0-9a-z_]，安全文件名
    out_dir = Path(SNAPSHOT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / file_name).write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()

    if t_captured_ms is not None:
        t_captured_ms = int(t_captured_ms)
    cur = conn.execute(
        """INSERT INTO snapshots
           (request_id, device_mac, boot_id, source, t_captured_ms,
            t_server_recv_ms, file_name, content_type, sha256, n_bytes,
            width, height)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rid, (device_mac or "").upper(), boot_id, source, t_captured_ms,
         now, file_name, content_type.strip(), sha, len(data),
         width, height),
    )
    conn.commit()
    out = dict(conn.execute(
        "SELECT * FROM snapshots WHERE id = ?", (cur.lastrowid,)
    ).fetchone())
    prune(conn, now=now)
    return out


def by_request(conn, request_id):
    """某条指令的唯一图像记录（无则 None）。done 回执前必须查它。"""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM snapshots WHERE request_id = ?", (request_id,)
    ).fetchone()
    return dict(row) if row else None


def get(conn, snapshot_id):
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM snapshots WHERE id = ?", (int(snapshot_id),)
    ).fetchone()
    return dict(row) if row else None


def list_snapshots(conn, limit=50, request_id=None):
    ensure_schema(conn)
    where, params = [], []
    if request_id:
        where.append("request_id = ?")
        params.append(request_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    rows = conn.execute(
        "SELECT * FROM snapshots %s ORDER BY t_server_recv_ms DESC, id DESC LIMIT ?"
        % clause, params,
    ).fetchall()
    return [dict(r) for r in rows]


def _meta(row):
    """元数据对外视图：不外露文件路径，只有引用与鉴证信息。"""
    return {
        "id": row["id"],
        "request_id": row["request_id"],
        "device_mac": row["device_mac"],
        "source": row["source"],
        "t_captured_ms": row["t_captured_ms"],
        "t_server_recv_ms": row["t_server_recv_ms"],
        "content_type": row["content_type"],
        "sha256": row["sha256"],
        "n_bytes": row["n_bytes"],
        "width": row["width"],
        "height": row["height"],
    }


def path_for(row):
    """磁盘文件路径。file_name 由 request_id + 白名单扩展名拼成，无穿越风险。"""
    return Path(SNAPSHOT_DIR) / row["file_name"]


# ---------------------------------------------------------------- 最少保留
def prune(conn, now=None):
    """超出数量/时间上限的旧图像：删文件 + 删记录。

    只在每次入库后惰性调用（与指令超时的 lazy sweep 同一思路），
    不需要后台线程。返回本次删除的条数。
    """
    ensure_schema(conn)
    now = now_ms() if now is None else now
    doomed = []
    rows = conn.execute(
        "SELECT * FROM snapshots ORDER BY t_server_recv_ms DESC, id DESC"
    ).fetchall()
    for i, r in enumerate(rows):
        too_many = i >= SNAPSHOT_KEEP_MAX
        too_old = now - r["t_server_recv_ms"] > SNAPSHOT_KEEP_MS
        if too_many or too_old:
            doomed.append(dict(r))
    for r in doomed:
        try:
            p = path_for(r)
            if p.exists():
                p.unlink()
        except OSError:
            pass
        conn.execute("DELETE FROM snapshots WHERE id = ?", (r["id"],))
    if doomed:
        conn.commit()
    return len(doomed)