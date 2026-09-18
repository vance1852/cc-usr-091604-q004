"""领域模型：裁判、比赛、指派、利益冲突与候选报告。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum, IntEnum


class RefereeLevel(IntEnum):
    """裁判等级，数值越大等级越高。"""

    LEVEL_3 = 1
    LEVEL_2 = 2
    LEVEL_1 = 3
    NATIONAL = 4
    INTERNATIONAL = 5

    @property
    def label(self) -> str:
        return _REFEREE_LEVEL_LABELS[int(self)]


_REFEREE_LEVEL_LABELS = {
    1: "国家三级",
    2: "国家二级",
    3: "国家一级",
    4: "国家级",
    5: "国际级",
}


class MatchLevel(IntEnum):
    """比赛级别，数值越大越重要，对裁判等级要求越高。"""

    GROUP = 1
    PLAYOFF = 2
    SEMIFINAL = 3
    FINAL = 4

    @property
    def label(self) -> str:
        return _MATCH_LEVEL_LABELS[int(self)]


_MATCH_LEVEL_LABELS = {1: "小组赛", 2: "季后赛", 3: "半决赛", 4: "决赛"}


class ConflictKind(Enum):
    """利益冲突类型（裁判主动申报）。"""

    TRAINING = "培训关系"
    RELATIVE = "亲属关系"
    EMPLOYMENT = "任职关系"
    FINANCIAL = "经济利益"
    OTHER = "其他"


class AssignmentStatus(Enum):
    """指派状态机。值即中文标签，便于导出。"""

    LOCKED = "已锁定"
    CONFIRMED = "已确认"
    ACCEPTED = "已接受"
    DECLINED = "已拒绝"
    REVOKED = "已撤销"
    SUPERSEDED = "已被替补"
    FLAGGED_CONFLICT = "冲突待复核"


#: 仍然占用裁判档期、参与“重复占用”判定的状态。
BOOKING_STATUSES = frozenset(
    {
        AssignmentStatus.LOCKED,
        AssignmentStatus.CONFIRMED,
        AssignmentStatus.ACCEPTED,
        AssignmentStatus.FLAGGED_CONFLICT,
    }
)

#: 可以被替补接管的状态。
SUBSTITUTABLE_STATUSES = frozenset(
    {
        AssignmentStatus.DECLINED,
        AssignmentStatus.REVOKED,
        AssignmentStatus.FLAGGED_CONFLICT,
    }
)


class MatchStatus(Enum):
    SCHEDULED = "未开赛"
    FINISHED = "已结束"
    CANCELLED = "已取消"


@dataclass
class TimeWindow:
    """绝对 UTC 区间；跨天时段由调用方展开后自然落在此结构上。"""

    start: datetime
    end: datetime
    reason: str = ""

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0


@dataclass
class Referee:
    """裁判档案：等级、可执法项目、执法区域、不可用时段。"""

    id: str
    name: str
    level: RefereeLevel
    events: frozenset[str]
    regions: frozenset[str]
    home_city: str
    max_daily_minutes: int | None = None
    unavailable: list[TimeWindow] = field(default_factory=list)


@dataclass
class ConflictOfInterest:
    """裁判主动申报的利益冲突。"""

    id: str
    referee_id: str
    team_id: str
    kind: ConflictKind
    note: str
    declared_at: datetime
    active: bool = True


@dataclass
class Match:
    """一场比赛。时间以 UTC 存储，``tz`` 保留赛地时区用于展示与导出。"""

    id: str
    name: str
    level: MatchLevel
    event: str
    region: str
    city: str
    tz: str
    start_utc: datetime
    end_utc: datetime
    teams: tuple[str, ...]
    slots_required: int = 1
    status: MatchStatus = MatchStatus.SCHEDULED
    version: int = 0
    history: list["AssignmentEvent"] = field(default_factory=list)

    @property
    def duration_minutes(self) -> float:
        return (self.end_utc - self.start_utc).total_seconds() / 60.0


@dataclass
class AssignmentEvent:
    """指派时间线事件：每次状态变化都追加，永不删除。"""

    seq: int
    at: datetime
    actor: str
    action: str
    reason: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["at"] = self.at.isoformat()
        return data


@dataclass
class Assignment:
    """一次指派（一名裁判占一个席位）。"""

    id: str
    match_id: str
    referee_id: str
    slot: int
    status: AssignmentStatus
    created_at: datetime
    timeline: list[AssignmentEvent] = field(default_factory=list)


@dataclass
class Exclusion:
    """候选被排除的原因。

    ``category`` 区分 ``qualification``（资格不符）、``availability``
    （档期/通勤冲突）与 ``conflict``（利益冲突规则），接口据此区分
    “未找到合格人选”与“被冲突规则排除”。
    """

    referee_id: str
    referee_name: str
    code: str
    category: str
    detail: str
    conflict_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Candidate:
    """合格候选及其打分明细（分数越低越优先）。"""

    referee_id: str
    referee_name: str
    score: float
    distance_km: float
    recent_matches: int
    day_minutes: float
    breakdown: dict

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CandidateReport:
    """候选名单结果。

    ``empty_reason`` 在没有合格候选时区分：
    ``NO_QUALIFIED_CANDIDATES``（无人通过资格/档期筛选）与
    ``ALL_EXCLUDED_BY_CONFLICT``（有合格人选但全部被冲突规则排除）。
    """

    match_id: str
    candidates: list[Candidate]
    excluded: list[Exclusion]
    empty_reason: str | None

    def to_dict(self) -> dict:
        return {
            "match_id": self.match_id,
            "candidates": [c.to_dict() for c in self.candidates],
            "excluded": [e.to_dict() for e in self.excluded],
            "empty_reason": self.empty_reason,
        }
