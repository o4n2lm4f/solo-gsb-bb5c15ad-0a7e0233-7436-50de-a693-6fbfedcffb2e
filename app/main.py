"""FastAPI 应用：跨时区可用时段聚合。

启动（容器内）：uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import scheduling
from .scheduling import SchedulingError
from .schemas import (
    AggregationRequest,
    AggregationResponse,
    LocalTimeOut,
    ParticipantIn,
    SegmentOut,
    WEEKDAY_NAMES,
)
from .scheduling import Participant, Window as CoreWindow

app = FastAPI(
    title="跨时区可用时段聚合 API",
    description=(
        "接收日期范围、会议时长、最低出席人数及每位参与者按本地星期/时刻填写的 "
        "重复可用区间与 IANA 时区，返回可安排会议的 UTC 候选时段及各自本地时刻。"
        "DST 按具体日期应用：不存在的本地时刻返回 400，歧义时刻统一取 fold=0。"
    ),
    version="1.0.0",
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
