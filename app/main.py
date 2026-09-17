"""FastAPI 应用：跨时区可用时段聚合 + SQLite 会议预约。

启动（容器内）：uvicorn app.main:app --host 0.0.0.0 --port 8000
启动时自动初始化预约数据库（路径见环境变量 ``RESERVATION_DB_PATH``）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from . import reservations as res_store
from . import scheduling
from .reservations import (
    ConflictingReservation,
    IdempotencyConflict,
    OverlapConflict,
    ReservationStore,
    from_micros,
    to_micros,
)
from .scheduling import SchedulingError
from .schemas import (
    AggregationRequest,
    AggregationResponse,
    LocalTimeOut,
    ParticipantIn,
    ReservationRequest,
    ReservationResponse,
    SegmentOut,
    WEEKDAY_NAMES,
)
from .scheduling import Participant, Window as CoreWindow


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Compose/容器启动时在此自动建库建表；路径由 RESERVATION_DB_PATH 配置，
    # 缺省落到仓库内 data/reservations.db（容器内为 /data 命名卷）。
    res_store.init_store()
    yield


app = FastAPI(
    title="跨时区会议排期 API",
    description=(
        "两类能力：\n\n"
        "1. 接收日期范围、会议时长、最低出席人数及每位参与者按本地星期/时刻填写的 "
        "重复可用区间与 IANA 时区，返回可安排会议的 UTC 候选时段及各自本地时刻。"
        "DST 按具体日期应用：不存在的本地时刻返回 400，歧义时刻统一取 fold=0。\n"
        "2. POST /api/v1/reservations：在 SQLite 中原子化写入会议预约，"
        "半开区间重叠返回 409，request_id 提供幂等保证。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.exception_handler(SchedulingError)
async def scheduling_error_handler(request, exc: SchedulingError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _to_core_participant(p: ParticipantIn) -> Participant:
    return Participant(
        participant_id=p.id,
        timezone=p.timezone,
        windows=tuple(
            CoreWindow(weekday=w.weekday,
                       start_local=w.start_local,
                       end_local=w.end_local)
            for w in p.availability
        ),
    )


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_utc_precise(dt: datetime) -> str:
    """预约时间戳按微秒精度回显（无小数秒时仍为整洁的 Z 形式）。"""
    dt_utc = dt.astimezone(timezone.utc)
    if dt_utc.microsecond:
        return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0") + "Z"
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _segment_to_out(seg: scheduling.Segment,
                    tz_by_id: dict[str, ZoneInfo]) -> SegmentOut:
    local_times: list[LocalTimeOut] = []
    for pid in seg.attendees:
        tzinfo = tz_by_id[pid]
        start_local = seg.start_utc.astimezone(tzinfo)
        end_local = seg.end_utc.astimezone(tzinfo)
        local_times.append(LocalTimeOut(
            participant_id=pid,
            timezone=str(tzinfo),
            start_local=start_local.isoformat(),
            end_local=end_local.isoformat(),
            start_weekday=WEEKDAY_NAMES[start_local.isoweekday()],
            end_weekday=WEEKDAY_NAMES[end_local.isoweekday()],
        ))
    return SegmentOut(
        start_utc=_iso_utc(seg.start_utc),
        end_utc=_iso_utc(seg.end_utc),
        duration_minutes=int(seg.duration.total_seconds() // 60),
        attendees=list(seg.attendees),
        attendee_count=len(seg.attendees),
        local_times=local_times,
    )


@app.post("/api/v1/availability/aggregate", response_model=AggregationResponse)
async def aggregate_availability(req: AggregationRequest) -> AggregationResponse:
    participants = [_to_core_participant(p) for p in req.participants]
    # 提前解析所有时区，未知名称统一返回 400
    tz_by_id = {p.participant_id: scheduling.get_timezone(p.timezone)
                for p in participants}

    segments = scheduling.aggregate(
        participants=participants,
        start_date=req.start_date,
        end_date=req.end_date,
        meeting_minutes=req.meeting_minutes,
        min_attendees=req.min_attendees,
    )

    return AggregationResponse(
        start_date=req.start_date,
        end_date=req.end_date,
        meeting_minutes=req.meeting_minutes,
        min_attendees=req.min_attendees,
        segments=[_segment_to_out(seg, tz_by_id) for seg in segments],
    )


# ----------------------------------------------------------- 会议预约

def _reservation_to_out(record, created: bool) -> ReservationResponse:
    return ReservationResponse(
        id=record.id,
        request_id=record.request_id,
        start_utc=_iso_utc_precise(from_micros(record.start_us)),
        end_utc=_iso_utc_precise(from_micros(record.end_us)),
        participant_ids=list(record.participant_ids),
        participant_count=len(record.participant_ids),
        status="created" if created else "replayed",
    )


def _conflict_out(c: ConflictingReservation) -> dict:
    return {
        "reservation_id": c.reservation_id,
        "request_id": c.request_id,
        "start_utc": _iso_utc_precise(from_micros(c.start_us)),
        "end_utc": _iso_utc_precise(from_micros(c.end_us)),
        "participant_ids": list(c.participant_ids),
    }


@app.post(
    "/api/v1/reservations",
    response_model=ReservationResponse,
    status_code=201,
    responses={
        200: {"model": ReservationResponse,
              "description": "相同 request_id 同内容的幂等重试，返回原预约"},
        409: {"description": "参与者时间冲突，或 request_id 被不同内容复用"},
    },
)
def create_reservation(
    req: ReservationRequest,
    store: ReservationStore = Depends(res_store.get_store),
):
    """原子化创建会议预约。

    - 单事务检查并写入全部参与者占用，任一参与者半开区间重叠则整体拒绝（409），
      无部分写入；首尾相接的相邻区间不冲突。
    - 相同 request_id + 相同规范化请求（UTC 起止时刻、去重排序后的参与者）
      重试返回原预约（200，status=replayed），不新增记录。
    - 相同 request_id 但内容不同返回 409。
    """
    participant_ids = req.normalized_participant_ids
    start_us = to_micros(req.start_utc)
    end_us = to_micros(req.end_utc)

    try:
        record, created = store.create_reservation(
            request_id=req.request_id,
            start_us=start_us,
            end_us=end_us,
            participant_ids=participant_ids,
        )
    except IdempotencyConflict as exc:
        existing = exc.reservation
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "type": "idempotency_conflict",
                "request_id": req.request_id,
                "existing_reservation": _reservation_to_out(existing, False).model_dump(),
            },
        )
    except OverlapConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "reservation conflicts with one or more existing reservations",
                "type": "overlap",
                "conflicts": [_conflict_out(c) for c in exc.conflicts],
            },
        )

    # 幂等重放返回 200，首次创建返回 201
    return JSONResponse(
        status_code=201 if created else 200,
        content=_reservation_to_out(record, created).model_dump(),
    )
