"""POST /api/v1/reservations 接口测试：校验、冲突、幂等与启动初始化。"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import reservations as res_store
from app.main import app

UTC = timezone.utc


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 通过环境变量指到临时库：lifespan 启动时 init_store() 也会用这个路径，
    # 不会覆盖成默认的 data/reservations.db
    db_path = tmp_path / "reservations.db"
    monkeypatch.setenv("RESERVATION_DB_PATH", str(db_path))
    res_store._store = None
    try:
        with TestClient(app) as c:
            yield c
    finally:
        res_store._store = None


def _payload(**overrides):
    payload = {
        "request_id": "req-001",
        "start_utc": "2026-03-09T13:00:00Z",
        "end_utc": "2026-03-09T14:00:00Z",
        "participant_ids": ["alice", "bob"],
    }
    payload.update(overrides)
    return payload


# ------------------------------------------------------------------ 生命周期

def test_lifespan_initializes_database_at_configured_path(tmp_path, monkeypatch):
    db_path = tmp_path / "subdir" / "bookings.db"
    monkeypatch.setenv("RESERVATION_DB_PATH", str(db_path))
    res_store._store = None
    try:
        with TestClient(app):
            assert db_path.exists()
            conn = sqlite3.connect(db_path)
            try:
                tables = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                assert {"reservations", "reservation_participants"} <= tables
            finally:
                conn.close()
    finally:
        res_store._store = None


def test_lazy_get_store_without_lifespan(tmp_path, monkeypatch):
    # 未走 lifespan 时 get_store() 也能按环境变量懒初始化（且不死锁）
    db_path = tmp_path / "lazy.db"
    monkeypatch.setenv("RESERVATION_DB_PATH", str(db_path))
    res_store._store = None
    try:
        store = res_store.get_store()
        assert isinstance(store, res_store.ReservationStore)
        assert db_path.exists()
        # 同一单例
        assert res_store.get_store() is store
    finally:
        res_store._store = None


# ------------------------------------------------------------------ 创建

def test_create_reservation_201(client):
    resp = client.post("/api/v1/reservations", json=_payload())
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["id"].startswith("res_")
    assert data["request_id"] == "req-001"
    assert data["start_utc"] == "2026-03-09T13:00:00Z"
    assert data["end_utc"] == "2026-03-09T14:00:00Z"
    assert data["participant_ids"] == ["alice", "bob"]
    assert data["participant_count"] == 2
    assert data["status"] == "created"


def test_participants_deduped_and_sorted(client):
    resp = client.post(
        "/api/v1/reservations",
        json=_payload(participant_ids=["bob", "alice", "bob", "carol"]),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["participant_ids"] == ["alice", "bob", "carol"]


def test_timezone_offset_normalized_to_utc(client):
    # 14:00+09:00 == 05:00Z
    resp = client.post(
        "/api/v1/reservations",
        json=_payload(
            request_id="req-tz",
            start_utc="2026-03-09T14:00:00+09:00",
            end_utc="2026-03-09T15:00:00+09:00",
            participant_ids=["alice"],
        ),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["start_utc"] == "2026-03-09T05:00:00Z"
    assert data["end_utc"] == "2026-03-09T06:00:00Z"


def test_subsecond_precision_echoed(client):
    resp = client.post(
        "/api/v1/reservations",
        json=_payload(
            request_id="req-us",
            start_utc="2026-03-09T13:00:00.123456Z",
            end_utc="2026-03-09T14:00:00.654321Z",
            participant_ids=["alice"],
        ),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["start_utc"] == "2026-03-09T13:00:00.123456Z"
    assert data["end_utc"] == "2026-03-09T14:00:00.654321Z"


# ------------------------------------------------------------------ 冲突

def test_overlap_returns_409_with_conflict_detail(client):
    first = client.post("/api/v1/reservations",
                        json=_payload(request_id="req-a",
                                      participant_ids=["alice", "bob"]))
    assert first.status_code == 201
    first_id = first.json()["id"]

    resp = client.post(
        "/api/v1/reservations",
        json=_payload(request_id="req-b", participant_ids=["bob", "carol"]),
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["type"] == "overlap"
    assert len(body["conflicts"]) == 1
    conflict = body["conflicts"][0]
    assert conflict["reservation_id"] == first_id
    assert conflict["request_id"] == "req-a"
    assert conflict["participant_ids"] == ["bob"]
    assert conflict["start_utc"] == "2026-03-09T13:00:00Z"
    assert conflict["end_utc"] == "2026-03-09T14:00:00Z"


def test_adjacent_intervals_are_accepted(client):
    assert client.post("/api/v1/reservations", json=_payload()).status_code == 201
    # 紧接其后
    resp = client.post(
        "/api/v1/reservations",
        json=_payload(
            request_id="req-next",
            start_utc="2026-03-09T14:00:00Z",
            end_utc="2026-03-09T15:00:00Z",
        ),
    )
    assert resp.status_code == 201, resp.text
    # 紧贴其前
    resp = client.post(
        "/api/v1/reservations",
        json=_payload(
            request_id="req-prev",
            start_utc="2026-03-09T12:00:00Z",
            end_utc="2026-03-09T13:00:00Z",
        ),
    )
    assert resp.status_code == 201, resp.text


def test_unrelated_participants_same_slot_accepted(client):
    client.post("/api/v1/reservations", json=_payload())
    resp = client.post(
        "/api/v1/reservations",
        json=_payload(request_id="req-other", participant_ids=["carol", "dave"]),
    )
    assert resp.status_code == 201, resp.text


# ------------------------------------------------------------------ 幂等

def test_idempotent_retry_returns_200_original_record(client):
    first = client.post("/api/v1/reservations", json=_payload())
    assert first.status_code == 201

    retry = client.post(
        "/api/v1/reservations",
        json=_payload(participant_ids=["bob", "alice"]),  # 顺序不同
    )
    assert retry.status_code == 200, retry.text
    r = retry.json()
    assert r["id"] == first.json()["id"]
    assert r["status"] == "replayed"
    assert r["participant_ids"] == ["alice", "bob"]


def test_idempotent_retry_with_equivalent_offset(client):
    client.post("/api/v1/reservations", json=_payload())
    retry = client.post(
        "/api/v1/reservations",
        json=_payload(
            start_utc="2026-03-09T22:00:00+09:00",  # == 13:00Z
            end_utc="2026-03-09T09:00:00-05:00",    # == 14:00Z
        ),
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "replayed"


def test_same_request_id_different_payload_returns_409(client):
    first = client.post("/api/v1/reservations", json=_payload())
    resp = client.post(
        "/api/v1/reservations",
        json=_payload(end_utc="2026-03-09T15:00:00Z"),
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["type"] == "idempotency_conflict"
    assert body["request_id"] == "req-001"
    assert body["existing_reservation"]["id"] == first.json()["id"]


def test_same_request_id_different_participants_returns_409(client):
    client.post("/api/v1/reservations", json=_payload())
    resp = client.post(
        "/api/v1/reservations",
        json=_payload(participant_ids=["alice", "carol"]),
    )
    assert resp.status_code == 409
    assert resp.json()["type"] == "idempotency_conflict"


# ------------------------------------------------------------------ 422 校验

def test_end_before_start_422(client):
    resp = client.post(
        "/api/v1/reservations",
        json=_payload(start_utc="2026-03-09T15:00:00Z",
                      end_utc="2026-03-09T14:00:00Z"),
    )
    assert resp.status_code == 422


def test_end_equal_start_422(client):
    resp = client.post(
        "/api/v1/reservations",
        json=_payload(end_utc="2026-03-09T13:00:00Z"),
    )
    assert resp.status_code == 422


def test_naive_timestamp_422(client):
    resp = client.post(
        "/api/v1/reservations",
        json=_payload(start_utc="2026-03-09T13:00:00"),
    )
    assert resp.status_code == 422


def test_empty_participants_422(client):
    resp = client.post(
        "/api/v1/reservations",
        json=_payload(participant_ids=[]),
    )
    assert resp.status_code == 422


def test_blank_request_id_422(client):
    resp = client.post(
        "/api/v1/reservations",
        json=_payload(request_id=""),
    )
    assert resp.status_code == 422


# ------------------------------------------------------------------ 持久化

def test_records_survive_store_recreation(tmp_path):
    # 模拟容器重启：关闭后用同一路径重新打开，数据必须仍在
    db_path = str(tmp_path / "reservations.db")
    store1 = res_store.ReservationStore(db_path)
    store1.create_reservation(
        "persist-1",
        res_store.to_micros(datetime(2026, 3, 9, 13, tzinfo=UTC)),
        res_store.to_micros(datetime(2026, 3, 9, 14, tzinfo=UTC)),
        ["alice"],
    )
    store2 = res_store.ReservationStore(db_path)
    rec = store2.get_by_request_id("persist-1")
    assert rec is not None and rec.participant_ids == ("alice",)


# ------------------------------------------------------------------ 并发（经 API）

def test_concurrent_api_requests_no_partial_writes(client):
    n = 12
    barrier = threading.Barrier(n)
    statuses: list[int] = []

    def worker(i: int) -> None:
        barrier.wait()
        resp = client.post(
            "/api/v1/reservations",
            json=_payload(request_id=f"c-{i}",
                          participant_ids=["alice", f"extra-{i}"]),
        )
        statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses.count(201) == 1
    assert statuses.count(409) == n - 1
    # 唯一成功的请求会连带占用它的 extra；若发生过半写入，这里会有更多 extra 被占。
    # 用全部 extra 再订一次：冲突必须恰好指向 1 名 extra（赢家的）。
    resp = client.post(
        "/api/v1/reservations",
        json=_payload(request_id="check-extras",
                      participant_ids=[f"extra-{i}" for i in range(n)]),
    )
    assert resp.status_code == 409
    blocked = {
        pid
        for c in resp.json()["conflicts"]
        for pid in c["participant_ids"]
    }
    assert len(blocked) == 1
    winner_extra = next(iter(blocked))
    # 其余所有 extra（输家的）都没有部分写入，可在同一时段成功预约
    losers = [f"extra-{i}" for i in range(n)
              if f"extra-{i}" != winner_extra]
    ok = client.post("/api/v1/reservations",
                     json=_payload(request_id="check-losers",
                                   participant_ids=losers))
    assert ok.status_code == 201, ok.text
    assert ok.json()["participant_count"] == n - 1
