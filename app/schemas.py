"""请求/响应 Pydantic 模型。"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# 1=周一 ... 7=周日（对应 date.isoweekday）
WEEKDAY_NAMES = {
    1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday",
    5: "Friday", 6: "Saturday", 7: "Sunday",
}
WEEKDAY_FROM_NAME = {v.lower(): k for k, v in WEEKDAY_NAMES.items()}
WEEKDAY_ALIASES = {
    "mon": 1, "tue": 2, "wed": 3, "thu": 4,
    "fri": 5, "sat": 6, "sun": 7,
}


def _parse_weekday(value: int | str) -> int:
    if isinstance(value, int):
        if value not in WEEKDAY_NAMES:
            raise ValueError("weekday must be 1 (Monday) .. 7 (Sunday)")
        return value
    key = value.strip().lower()
    if key in WEEKDAY_FROM_NAME:
        return WEEKDAY_FROM_NAME[key]
    if key in WEEKDAY_ALIASES:
        return WEEKDAY_ALIASES[key]
    raise ValueError(f"invalid weekday: {value!r}")


class AvailabilityWindowIn(BaseModel):
    """按本地星期与钟面时刻填写的每周重复可用区间（半开）。

    ``end_local <= start_local`` 表示跨午夜：窗口从 weekday 当天的
    start_local 持续到次日的 end_local。
    """

    weekday: int | str = Field(
        description="1..7（1=周一），或 'Monday'/'mon' 这样的名称",
        examples=[1],
    )
    start_local: time = Field(description="本地开始钟面时刻 HH:MM[:SS]", examples=["09:00"])
    end_local: time = Field(description="本地结束钟面时刻；<= 开始时刻表示跨午夜", examples=["17:30"])

    @field_validator("weekday")
    @classmethod
    def _validate_weekday(cls, v: int | str) -> int:
        return _parse_weekday(v)


class ParticipantIn(BaseModel):
    id: str = Field(min_length=1, description="参与者唯一标识", examples=["alice"])
    timezone: str = Field(description="IANA 时区名，如 Asia/Shanghai", examples=["Asia/Shanghai"])
    availability: list[AvailabilityWindowIn] = Field(min_length=1)


class AggregationRequest(BaseModel):
    start_date: date = Field(description="搜索日期范围起点（含，UTC）", examples=["2026-03-07"])
    end_date: date = Field(description="搜索日期范围终点（含，UTC）", examples=["2026-03-09"])
    meeting_minutes: int = Field(gt=0, description="会议时长（分钟）", examples=[60])
    min_attendees: int = Field(gt=0, description="最低出席人数", examples=[2])
    participants: list[ParticipantIn] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate(self) -> "AggregationRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        ids = [p.id for p in self.participants]
        if len(ids) != len(set(ids)):
            raise ValueError("participant ids must be unique")
        if self.min_attendees > len(self.participants):
            raise ValueError("min_attendees cannot exceed the number of participants")
        return self


class LocalTimeOut(BaseModel):
    participant_id: str
    timezone: str
    start_local: str = Field(description="本地开始时刻（含 UTC 偏移），如 2026-03-08T10:00:00+09:00")
    end_local: str
    start_weekday: Literal["Monday", "Tuesday", "Wednesday", "Thursday",
                           "Friday", "Saturday", "Sunday"]
    end_weekday: Literal["Monday", "Tuesday", "Wednesday", "Thursday",
                         "Friday", "Saturday", "Sunday"]


class SegmentOut(BaseModel):
    start_utc: str = Field(description="UTC 开始时间 ISO 8601", examples=["2026-03-08T01:00:00Z"])
    end_utc: str
    duration_minutes: int
    attendees: list[str] = Field(description="出席者 id，按字典序排列")
    attendee_count: int
    local_times: list[LocalTimeOut] = Field(description="每位出席者在各自时区的本地时刻")


class AggregationResponse(BaseModel):
    start_date: date
    end_date: date
    meeting_minutes: int
    min_attendees: int
    segments: list[SegmentOut]


# ------------------------------------------------------------ 会议预约

class ReservationRequest(BaseModel):
    """会议预约请求。

    - ``start_utc`` / ``end_utc`` 必须携带时区偏移（naive 时间一律拒绝），
      内部统一归一化到 UTC 比较，因此 ``Z`` 与 ``+00:00`` 等价；
    - 结束时刻必须严格晚于开始时刻；
    - 参与者 ID 自动去重并按字典序排列，作为幂等比较与占用写入的规范化形式。
    """

    request_id: str = Field(
        min_length=1,
        description="客户端幂等键；同键重试返回原预约，同键不同内容返回 409",
        examples=["req-20260309-001"],
    )
    start_utc: datetime = Field(
        description="会议开始时刻（必须含时区偏移，如 2026-03-09T13:00:00Z）",
        examples=["2026-03-09T13:00:00Z"],
    )
    end_utc: datetime = Field(
        description="会议结束时刻（必须晚于开始）",
        examples=["2026-03-09T14:00:00Z"],
    )
    participant_ids: list[str] = Field(
        min_length=1,
        description="参与者 ID 列表；重复 ID 会被去重，最终按字典序排列",
        examples=[["alice", "bob"]],
    )

    @field_validator("start_utc", "end_utc")
    @classmethod
    def _aware_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("timestamps must include a timezone offset (use UTC 'Z' suffix)")
        return v.astimezone(timezone.utc)

    @field_validator("participant_ids")
    @classmethod
    def _nonempty_participant(cls, v: list[str]) -> list[str]:
        if any(not pid or not pid.strip() for pid in v):
            raise ValueError("participant ids must be non-empty strings")
        return v

    @model_validator(mode="after")
    def _validate(self) -> "ReservationRequest":
        if self.end_utc <= self.start_utc:
            raise ValueError("end_utc must be strictly after start_utc")
        return self

    @property
    def normalized_participant_ids(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.participant_ids)))


class ReservationResponse(BaseModel):
    id: str = Field(description="预约 ID", examples=["res_8f14c2a0e93c4e7f9b6d1a2c3e4f5061"])
    request_id: str
    start_utc: str = Field(description="UTC 开始时间 ISO 8601（Z 后缀）")
    end_utc: str
    participant_ids: list[str] = Field(description="去重并按字典序排列的参与者 ID")
    participant_count: int
    status: Literal["created", "replayed"] = Field(
        description="created=新建预约；replayed=同 request_id 同内容的幂等重试"
    )
