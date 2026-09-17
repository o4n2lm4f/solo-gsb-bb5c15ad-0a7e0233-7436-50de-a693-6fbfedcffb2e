"""SQLite 会议预约存储。

预约以 ``reservations``（预约主体）+ ``reservation_participants``
（预约-参与者占用）两张表持久化。时间一律换算为 UTC 后以 **Unix epoch 微秒整数**
存储，避免浮点/时区歧义。

冲突判定采用**半开区间** ``[start, end)``：仅当
``existing.start < new.end AND new.start < existing.end`` 时视为重叠，
因此首尾相接（一个会议结束时刻恰为另一个开始时刻）不冲突。

``create_reservation`` 在单个 ``BEGIN IMMEDIATE`` 事务内串行完成：
幂等检查 → 全参与者冲突检查 → 预约行与全部占用行写入。任何一步失败都整体
回滚，不会留下部分占用写入；写事务在数据库级互斥，并发请求同样满足原子性。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 默认数据库文件放在仓库根目录 data/ 下，便于本地开发；
# 容器内由 RESERVATION_DB_PATH 覆盖为 /data/reservations.db（命名卷持久化）。
DEFAULT_DB_PATH = str(
    Path(__file__).resolve().parent.parent / "data" / "reservations.db"
)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_BUSY_TIMEOUT_MS = 30_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reservations (
    id         TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    start_us   INTEGER NOT NULL,
    end_us     INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS reservation_participants (
    reservation_id TEXT NOT NULL
        REFERENCES reservations(id) ON DELETE CASCADE,
    participant_id TEXT NOT NULL,
    PRIMARY KEY (reservation_id, participant_id)
);

-- 按参与者反查占用区间
CREATE INDEX IF NOT EXISTS idx_rp_participant
    ON reservation_participants (participant_id, reservation_id);
"""


class ReservationError(Exception):
    """预约写入相关错误的基类。"""


@dataclass(frozen=True)
class ReservationRecord:
    """一条已落库的预约。participant_ids 已去重并按字典序排列。"""

    id: str
    request_id: str
    start_us: int
    end_us: int
    participant_ids: tuple[str, ...]


@dataclass(frozen=True)
class ConflictingReservation:
    """与新请求冲突的已有预约；``participant_ids`` 为双方参与者集合的交集
    （即真正造成冲突的那些人），已排序。"""

    reservation_id: str
    request_id: str
    start_us: int
    end_us: int
    participant_ids: tuple[str, ...]


class OverlapConflict(ReservationError):
    """至少一名参与者与已有预约存在半开区间重叠。"""

    def __init__(self, conflicts: list[ConflictingReservation]):
        super().__init__("one or more participants are already booked")
        self.conflicts = conflicts


class IdempotencyConflict(ReservationError):
    """相同 request_id 对应的已有预约内容（起止时刻/参与者）与本次请求不同。"""

    def __init__(self, reservation: ReservationRecord):
        super().__init__(
            f"request_id {reservation.request_id!r} was already used with a "
            "different payload"
        )
        self.reservation = reservation


def to_micros(dt: datetime) -> int:
    """aware datetime -> UTC Unix epoch 微秒（整数运算，不丢精度）。"""
    delta = dt.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def from_micros(us: int) -> datetime:
    """Unix epoch 微秒 -> UTC aware datetime。"""
    return _EPOCH + timedelta(microseconds=us)


class ReservationStore:
    """SQLite 预约存储。每个方法使用独立短连接，配合 WAL 支持并发读写。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        db_dir = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(db_dir, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None：显式管理事务，确保 BEGIN IMMEDIATE 不被驱动拦截
        conn = sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_MS / 1000,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        """启动时幂等建库建表；WAL 模式是数据库级持久属性，只需设置一次。"""
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    @contextmanager
    def _write_transaction(self):
        """BEGIN IMMEDIATE：立即获取写锁，把并发写事务串行化，
        从根上消除“两个事务同时检查通过后双写”的竞态。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _load(self, conn: sqlite3.Connection, row: sqlite3.Row) -> ReservationRecord:
        pids = tuple(sorted(
            r[0] for r in conn.execute(
                "SELECT participant_id FROM reservation_participants "
                "WHERE reservation_id = ? ORDER BY participant_id",
                (row["id"],),
            )
        ))
        return ReservationRecord(
            id=row["id"],
            request_id=row["request_id"],
            start_us=row["start_us"],
            end_us=row["end_us"],
            participant_ids=pids,
        )

    def get_by_request_id(self, request_id: str) -> ReservationRecord | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, request_id, start_us, end_us FROM reservations "
                "WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            return self._load(conn, row) if row is not None else None
        finally:
            conn.close()

    def _find_overlaps(self, conn: sqlite3.Connection, start_us: int,
                       end_us: int, participant_ids: list[str]
                       ) -> list[ConflictingReservation]:
        placeholders = ",".join("?" for _ in participant_ids)
        # 半开区间重叠：existing.start < new.end AND new.start < existing.end
        rows = conn.execute(
            f"""
            SELECT r.id, r.request_id, r.start_us, r.end_us,
                   GROUP_CONCAT(rp.participant_id) AS pids
            FROM reservations r
            JOIN reservation_participants rp ON rp.reservation_id = r.id
            WHERE rp.participant_id IN ({placeholders})
              AND r.start_us < ? AND ? < r.end_us
            GROUP BY r.id
            ORDER BY r.start_us, r.id
            """,
            (*participant_ids, end_us, start_us),
        ).fetchall()

        wanted = set(participant_ids)
        conflicts: list[ConflictingReservation] = []
        for row in rows:
            overlap_pids = tuple(sorted(set(row["pids"].split(",")) & wanted))
            if overlap_pids:
                conflicts.append(ConflictingReservation(
                    reservation_id=row["id"],
                    request_id=row["request_id"],
                    start_us=row["start_us"],
                    end_us=row["end_us"],
                    participant_ids=overlap_pids,
                ))
        return conflicts

    def create_reservation(
        self,
        request_id: str,
        start_us: int,
        end_us: int,
        participant_ids: list[str] | tuple[str, ...],
    ) -> tuple[ReservationRecord, bool]:
        """在单个事务内完成幂等检查、冲突检查与全部占用写入。

        返回 ``(记录, created)``：内容一致的幂等重试返回原记录且
        ``created=False``，不新增任何数据。

        - 内容不同的 request_id 复用 -> :class:`IdempotencyConflict`
        - 参与者存在半开区间重叠 -> :class:`OverlapConflict`
        """
        participants = sorted(set(participant_ids))
        try:
            with self._write_transaction() as conn:
                row = conn.execute(
                    "SELECT id, request_id, start_us, end_us FROM reservations "
                    "WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if row is not None:
                    existing = self._load(conn, row)
                    same_payload = (
                        existing.start_us == start_us
                        and existing.end_us == end_us
                        and existing.participant_ids == tuple(participants)
                    )
                    if not same_payload:
                        raise IdempotencyConflict(existing)
                    return existing, False

                conflicts = self._find_overlaps(conn, start_us, end_us, participants)
                if conflicts:
                    raise OverlapConflict(conflicts)

                reservation_id = "res_" + uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO reservations (id, request_id, start_us, end_us) "
                    "VALUES (?, ?, ?, ?)",
                    (reservation_id, request_id, start_us, end_us),
                )
                conn.executemany(
                    "INSERT INTO reservation_participants "
                    "(reservation_id, participant_id) VALUES (?, ?)",
                    [(reservation_id, pid) for pid in participants],
                )
                record = ReservationRecord(
                    id=reservation_id,
                    request_id=request_id,
                    start_us=start_us,
                    end_us=end_us,
                    participant_ids=tuple(participants),
                )
                return record, True
        except sqlite3.IntegrityError:
            # 兜底：极端竞态下 request_id UNIQUE 约束拦截到并发重复提交，
            # 转为按幂等语义重新判定（绝不报 500 或产生第二条记录）。
            existing = self.get_by_request_id(request_id)
            if existing is None:
                raise
            same_payload = (
                existing.start_us == start_us
                and existing.end_us == end_us
                and existing.participant_ids == tuple(participants)
            )
            if same_payload:
                return existing, False
            raise IdempotencyConflict(existing) from None


_store: ReservationStore | None = None
_store_lock = threading.Lock()


def init_store(db_path: str | None = None) -> ReservationStore:
    """应用启动时初始化（建库建表）并设置全局存储单例。"""
    global _store
    path = db_path or os.environ.get("RESERVATION_DB_PATH", DEFAULT_DB_PATH)
    store = ReservationStore(path)
    with _store_lock:
        _store = store
    return store


def get_store() -> ReservationStore:
    """懒加载全局存储单例；路径取 ``RESERVATION_DB_PATH``，缺省为仓库内 data/。"""
    global _store
    store = _store
    if store is None:
        with _store_lock:
            store = _store
            if store is None:
                path = os.environ.get("RESERVATION_DB_PATH", DEFAULT_DB_PATH)
                store = ReservationStore(path)
                _store = store
    return store
