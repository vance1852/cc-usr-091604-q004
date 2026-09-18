"""领域模型：裁判、比赛、不可用时段、利益冲突与指派记录。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from .timeutils import to_iso


class Grade(str, Enum):
    """裁判等级，数值越大可执法的比赛级别越高。"""

    LEVEL_3 = "国家三级"
    LEVEL_2 = "国家二级"
    LEVEL_1 = "国家一级"
    NATIONAL = "国家级"

    @property
    def rank(self) -> int:
        return {
            Grade.LEVEL_3: 3,
            Grade.LEVEL_2: 2,
            Grade.LEVEL_1: 1,
            Grade.NATIONAL: 0,
        }[self]

    @classmethod
    def of(cls, value: str | "Grade") -> "Grade":
        return value if isinstance(value, Grade) else cls(value)


# 比赛级别 -> 执法所需最低等级（rank 小于等于该值才算够格）
MATCH_MIN_GRADE: dict[str, Grade] = {
    "职业": Grade.NATIONAL,
    "甲级": Grade.LEVEL_1,
    "乙级": Grade.LEVEL_2,
    "业余": Grade.LEVEL_3,
    "青少年": Grade.LEVEL_3,
}


class ConflictType(str, Enum):
    TRAINING = "training"      # 在该队担任培训/执教
    FAMILY = "family"          # 直系亲属在该队
    EMPLOYMENT = "employment"  # 任职/挂靠该队或其赞助单位
    FINANCIAL = "financial"    # 经济利益
    OTHER = "other"


class UnavailabilityKind(str, Enum):
    PERSONAL = "personal"
    LEAGUE_BLOCK = "league_block"


@dataclass
class Official:
    """裁判档案：等级、可执法项目、执法区域（常驻城市+可覆盖城市）。"""

    id: str
    name: str
    grade: Grade
    home_city: str
    sports: frozenset[str] = frozenset({"篮球"})
    region_cities: frozenset[str] = frozenset()  # 除常驻城市外可执法的城市
    active: bool = True

    def can_ref_sport(self, sport: str) -> bool:
        return sport in self.sports

    def covers_city(self, city: str) -> bool:
        return city == self.home_city or city in self.region_cities


@dataclass
class Team:
    id: str
    name: str
    city: str


@dataclass
class Match:
    """比赛：级别、项目、主客队、场馆城市与跨时区起止时间。"""

    id: str
    level: str
    sport: str
    home_team_id: str
    away_team_id: str
    city: str
    venue: str
    start: datetime
    end: datetime
    tz_name: str = "Asia/Shanghai"
    # 比赛生命周期由 started_at / finished_at 判定
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    @property
    def is_finished(self) -> bool:
        return self.finished_at is not None

    @property
    def is_started(self) -> bool:
        return self.started_at is not None

    def duration_hours(self) -> float:
        return round((self.end - self.start).total_seconds() / 3600.0, 2)


@dataclass
class Unavailability:
    """裁判主动登记的不可用时段（带时区）。"""

    id: str
    official_id: str
    start: datetime
    end: datetime
    reason: str = ""
    kind: UnavailabilityKind = UnavailabilityKind.PERSONAL

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and start < self.end


@dataclass
class ConflictDeclaration:
    """裁判主动申报的利益冲突：与某队的培训/亲属/经济关系。"""

    id: str
    official_id: str
    team_id: str
    conflict_type: ConflictType
    detail: str = ""
    declared_at: Optional[datetime] = None
    active: bool = True


@dataclass
class AssignmentEvent:
    """指派时间线上的一条不可变事件。"""

    at: datetime
    actor: str
    action: str
    reason: str = ""
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "at": to_iso(self.at),
            "actor": self.actor,
            "action": self.action,
            "reason": self.reason,
            "detail": self.detail,
        }


class AssignmentStatus(str, Enum):
    CANDIDATE = "candidate"        # 候选（主任尚未确认）
    LOCKED = "locked"              # 主任已锁定候选，等待确认/裁判响应
    CONFIRMED = "confirmed"        # 主任已确认指派，等待裁判接受
    ACCEPTED = "accepted"         # 裁判已接受
    DECLINED = "declined"         # 裁判拒绝（带理由）
    CANCELLED = "cancelled"       # 撤销（改派替补等）
    REPLACED = "replaced"         # 被替补接管（原裁判保留在时间线）
    SUPERSEDED = "superseded"      # 批量/重评估中被新版本取代

    @property
    def occupies(self) -> bool:
        """该状态是否仍占用裁判（防止重复占用）。"""
        return self in _OCCUPYING

    @property
    def is_historical(self) -> bool:
        return self in (AssignmentStatus.ACCEPTED, AssignmentStatus.DECLINED)


_OCCUPYING = {
    AssignmentStatus.LOCKED,
    AssignmentStatus.CONFIRMED,
    AssignmentStatus.ACCEPTED,
}

_TERMINAL = {
    AssignmentStatus.ACCEPTED,
    AssignmentStatus.DECLINED,
    AssignmentStatus.CANCELLED,
    AssignmentStatus.REPLACED,
    AssignmentStatus.SUPERSEDED,
}


@dataclass
class Assignment:
    """一场比赛对一名裁判的指派记录，含完整状态时间线。"""

    id: str
    match_id: str
    official_id: str
    status: AssignmentStatus
    created_at: datetime
    lock_version: int = 1
    events: list[AssignmentEvent] = field(default_factory=list)
    response_reason: str = ""
    responded_at: Optional[datetime] = None
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[datetime] = None
    cancelled_reason: str = ""
    # 接管关系：替补记录指向被接管的旧指派
    replaces_assignment_id: Optional[str] = None

    def add_event(self, actor: str, action: str, reason: str = "", **detail) -> None:
        from .timeutils import now_utc

        self.events.append(
            AssignmentEvent(at=now_utc(), actor=actor, action=action, reason=reason, detail=detail)
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "match_id": self.match_id,
            "official_id": self.official_id,
            "status": self.status.value,
            "lock_version": self.lock_version,
            "response_reason": self.response_reason,
            "responded_at": to_iso(self.responded_at) if self.responded_at else None,
            "confirmed_by": self.confirmed_by,
            "confirmed_at": to_iso(self.confirmed_at) if self.confirmed_at else None,
            "cancelled_reason": self.cancelled_reason,
            "replaces_assignment_id": self.replaces_assignment_id,
            "created_at": to_iso(self.created_at),
            "timeline": [e.to_dict() for e in self.events],
        }
