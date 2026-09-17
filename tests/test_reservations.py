"""ReservationStore 单元测试：幂等、半开区间冲突、事务原子性与并发。"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from app.reservations import (
    ConflictingReservation,
    IdempotencyConflict,
    OverlapConflict,
    ReservationStore,
    from_micros,
    to_micros,
)

UTC = timezone.utc


def _dt(y, mo, d, h, mi=0, s=0, us=0):
    return datetime(y, mo, d, h, mi, s, us, tzinfo=UTC)


T0 = to_micros(_dt(2026, 3, 9, 13))
T1 = to_micros(_dt(2026, 3, 9, 14))
T2 = to_micros(_dt(2026, 3, 9, 15))


@pytest.fixture
def store(tmp_path):
    return ReservationStore(str(tmp_path / "reservations.db"))


def _raw_counts(db_path: str) -> tuple[int, int]:
    conn = sqlite3.connect(db_path)
    try:
        reservations = conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
        participants = conn.execute(
            "SELECT COUNT(*) FROM reservation_participants"
        ).fetchone()[0]
        return reservations, participants
    finally:
        conn.close()


# ------------------------------------------------------------------ 基本读写

def test_create_persists_and_normalizes_participants(store):
    rec, created = store.create_reservation("req-1", T0, T1, ["bob", "alice", "alice"])
    assert created is True
    assert rec.id.startswith("res_")
    assert rec.request_id == "req-1"
    assert rec.participant_ids == ("alice", "bob")

    loaded = store.get_by_request_id("req-1")
    assert loaded == rec


def test_get_unknown_request_id_returns_none(store):
    assert store.get_by_request_id("nope") is None


def test_creates_db_file_and_schema_is_idempotent(tmp_path):
    path = tmp_path / "nested" / "r.db"
    assert not path.exists()
    s1 = ReservationStore(str(path))
    assert path.exists()
    # 再次初始化（模拟重启）不应报错、不破坏数据
    s2 = ReservationStore(str(path))
    rec, created = s2.create_reservation("req-1", T0, T1, ["alice"])
    assert created


# ------------------------------------------------------------------ 幂等

def test_idempotent_retry_same_payload_returns_same_record(store):
    first, created1 = store.create_reservation("req-1", T0, T1, ["alice", "bob"])
    assert created1
    # 参与者顺序不同、重复出现，规范化后视为同一请求
    second, created2 = store.create_reservation("req-1", T0, T1, ["bob", "alice", "bob"])
    assert created2 is False
    assert second.id == first.id
    assert _raw_counts(store.db_path) == (1, 2)


def test_idempotent_retry_equivalent_offset(store):
    # 同一时刻的不同 UTC 偏移写法不应影响幂等比较
    t0_alt = to_micros(datetime(2026, 3, 9, 14, 0, tzinfo=timezone.utc))  # ==T1
    first, _ = store.create_reservation("req-1", T0, T1, ["alice"])
    second, created = store.create_reservation("req-1", T0, t0_alt, ["alice"])
    assert created is False
    assert second.id == first.id


def test_same_request_id_different_time_is_conflict(store):
    first, _ = store.create_reservation("req-1", T0, T1, ["alice"])
    with pytest.raises(IdempotencyConflict) as ei:
        store.create_reservation("req-1", T1, T2, ["alice"])
    assert ei.value.reservation.id == first.id


def test_same_request_id_different_participants_is_conflict(store):
    store.create_reservation("req-1", T0, T1, ["alice"])
    with pytest.raises(IdempotencyConflict):
        store.create_reservation("req-1", T0, T1, ["alice", "bob"])


def test_different_request_ids_are_independent(store):
    r1, c1 = store.create_reservation("req-1", T0, T1, ["alice", "bob"])
    r2, c2 = store.create_reservation("req-2", T0, T1, ["carol"])
    assert c1 and c2
    assert r1.id != r2.id


# -------------------------------------------------------------- 区间冲突语义

def test_overlap_any_participant_rejects_whole_request(store):
    store.create_reservation("req-1", T0, T1, ["carol"])
    # dave 空闲，但 carol 与已有区间重叠 -> 整体拒绝
    with pytest.raises(OverlapConflict) as ei:
        store.create_reservation("req-2", T0, T1, ["carol", "dave"])
    conflicts = ei.value.conflicts
    assert len(conflicts) == 1
    assert isinstance(conflicts[0], ConflictingReservation)
    assert conflicts[0].reservation_id == store.get_by_request_id("req-1").id
    assert conflicts[0].participant_ids == ("carol",)


def test_no_partial_write_on_conflict(store):
    store.create_reservation("req-1", T0, T1, ["alice"])
    with pytest.raises(OverlapConflict):
        store.create_reservation("req-2", T0, T1, ["alice", "bob", "carol"])
    # 只有第一条预约及其 1 名参与者落库
    assert _raw_counts(store.db_path) == (1, 1)
    # 未参与冲突的 bob/carol 不应留下任何占用：同时间段可以正常预约
    rec, created = store.create_reservation("req-3", T0, T1, ["bob", "carol"])
    assert created
    assert rec.participant_ids == ("bob", "carol")


def test_adjacent_intervals_do_not_conflict(store):
    # 三个首尾相接的区间：[T-1h,T0) [T0,T1) [T1,T2)
    pre = T0 - (T1 - T0)
    store.create_reservation("req-pre", pre, T0, ["alice"])
    store.create_reservation("req-1", T0, T1, ["alice"])
    # 后一个开始 == 前一个结束（相邻），半开区间不重叠
    rec, created = store.create_reservation("req-2", T1, T2, ["alice"])
    assert created
    # 相邻区间之间仍可插入参与者相同的边界检查：紧贴 req-pre 之前
    rec3, created3 = store.create_reservation("req-3", pre - (T1 - T0), pre,
                                              ["alice", "bob"])
    assert created3


def test_overlapping_participants_only_in_payload_are_reported(store):
    # 已有预约占用 alice+bob；新请求三人重叠，报告交集三人
    store.create_reservation("req-1", T0, T1, ["alice", "bob", "carol"])
    with pytest.raises(OverlapConflict) as ei:
        store.create_reservation("req-2", T0, T1, ["alice", "bob", "carol", "dave"])
    pids = ei.value.conflicts[0].participant_ids
    assert pids == ("alice", "bob", "carol")


def test_containing_and_partial_overlap_conflict(store):
    store.create_reservation("req-1", T0, T2, ["alice"])
    # 完全包含
    with pytest.raises(OverlapConflict):
        store.create_reservation("req-2", T0, T1, ["alice"])
    # 边界蹭到一点（新开始早于已有结束）
    with pytest.raises(OverlapConflict):
        store.create_reservation("req-3", to_micros(_dt(2026, 3, 9, 14, 30)),
                                 T2, ["alice"])


def test_microsecond_precision_roundtrip(store):
    t0 = to_micros(_dt(2026, 3, 9, 13, 0, 0, 123_456))
    t1 = to_micros(_dt(2026, 3, 9, 14, 0, 0, 654_321))
    rec, _ = store.create_reservation("req-1", t0, t1, ["alice"])
    loaded = store.get_by_request_id("req-1")
    assert loaded.start_us == t0 and loaded.end_us == t1
    assert from_micros(loaded.start_us).microsecond == 123_456
    # 微秒级重叠也要识别：与起点差 1us
    with pytest.raises(OverlapConflict):
        store.create_reservation("req-2", t0, t0 + 1, ["alice"])
    # 恰好在 end_us 处开始不冲突
    other, created = store.create_reservation("req-3", t1, t1 + 1_000_000, ["alice"])
    assert created


# ------------------------------------------------------------------ 并发

def test_concurrent_overlapping_requests_only_one_wins(store):
    n = 24
    results: list[str] = []
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            _, created = store.create_reservation(f"req-{i}", T0, T1, ["alice"])
            results.append("created" if created else "replayed")
        except OverlapConflict:
            results.append("conflict")
        except Exception as exc:  # 任何其他错误都不应出现
            results.append(f"error:{exc!r}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("created") == 1
    assert results.count("conflict") == n - 1
    assert all(not r.startswith("error") for r in results)
    assert _raw_counts(store.db_path) == (1, 1)


def test_concurrent_same_request_id_single_row(store):
    n = 16
    outcomes: list[tuple[bool, str]] = []
    barrier = threading.Barrier(n)

    def worker() -> None:
        barrier.wait()
        rec, created = store.create_reservation("req-dup", T0, T1, ["alice", "bob"])
        outcomes.append((created, rec.id))

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for created, _ in outcomes if created) == 1
    assert len({rid for _, rid in outcomes}) == 1
    assert _raw_counts(store.db_path) == (1, 2)


def test_concurrent_disjoint_participants_all_succeed(store):
    n = 10
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()
        store.create_reservation(f"req-{i}", T0, T1, [f"user-{i}"])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert _raw_counts(store.db_path) == (n, n)


def test_concurrent_partial_overlap_leaves_no_partial_rows(store):
    # 先占住 alice；之后 12 个并发请求都同时包含 alice + 各自独立参与者，
    # 全部必须失败，独立参与者也不能留下半条占用
    store.create_reservation("holder", T0, T1, ["alice"])
    barrier = threading.Barrier(12)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        barrier.wait()
        try:
            store.create_reservation(f"req-{i}", T0, T1, ["alice", f"extra-{i}"])
        except OverlapConflict as exc:
            errors.append(exc)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 12
    assert all(isinstance(e, OverlapConflict) for e in errors)
    reservations, participants = _raw_counts(store.db_path)
    assert reservations == 1
    assert participants == 1
