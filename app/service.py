"""裁判指派服务：候选生成、锁定/确认、响应、撤销、替补、批量、冲突重估与导出。

设计要点：

* 时间一律以 UTC 存储与比较，赛地时区仅用于展示、按日统计与导出；
* 每次指派变更都向时间线追加事件（带全局序号、时间、操作者、理由），
  已结束比赛的指派一律拒绝修改，保证历史可追溯、不可覆盖；
* 复合操作在 :class:`~app.repository.Repository` 事务内完成，
  确认指派时校验比赛版本号，并发确认只有一方成功。
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.errors import (
    InvalidStateError,
    MatchFinishedError,
    NotEligibleError,
    PlatformError,
    ReasonRequiredError,
    VersionConflictError,
)
from app.models import (
    BOOKING_STATUSES,
    SUBSTITUTABLE_STATUSES,
    Assignment,
    AssignmentEvent,
    AssignmentStatus,
    Candidate,
    CandidateReport,
    ConflictKind,
    ConflictOfInterest,
    Exclusion,
    Match,
    MatchLevel,
    MatchStatus,
    Referee,
    RefereeLevel,
    TimeWindow,
)
from app.repository import Repository
from app.timeutil import daily_window, ensure_utc, gap_minutes, haversine_km, overlaps

#: 内置城市坐标（可再注册补充），用于距离打分与跨城赶场判定。
DEFAULT_CITY_COORDS: dict[str, tuple[float, float]] = {
    "北京": (39.9042, 116.4074),
    "天津": (39.3434, 117.3616),
    "上海": (31.2304, 121.4737),
    "广州": (23.1291, 113.2644),
    "深圳": (22.5431, 114.0579),
    "成都": (30.5728, 104.0668),
    "杭州": (30.2741, 120.1551),
    "南京": (32.0603, 118.7969),
    "武汉": (30.5928, 114.3055),
    "西安": (34.3416, 108.9398),
    "重庆": (29.5630, 106.5516),
    "纽约": (40.7128, -74.0060),
}


@dataclass(frozen=True)
class ScoringWeights:
    """候选打分权重：分数越低越优先。"""

    distance_km: float = 1.0
    recent_match: float = 25.0
    day_minute: float = 0.5


@dataclass(frozen=True)
class PlatformConfig:
    """平台规则配置。"""

    min_level_by_match: dict[MatchLevel, RefereeLevel] = field(
        default_factory=lambda: {
            MatchLevel.GROUP: RefereeLevel.LEVEL_2,
            MatchLevel.PLAYOFF: RefereeLevel.LEVEL_1,
            MatchLevel.SEMIFINAL: RefereeLevel.NATIONAL,
            MatchLevel.FINAL: RefereeLevel.INTERNATIONAL,
        }
    )
    max_daily_minutes: int = 240  # 当日连续执法时长上限
    chain_gap_minutes: int = 30  # 间隔不超过该值视为“连续”工作
    recent_window_days: int = 14  # “近期场次”统计窗口
    same_city_buffer_minutes: int = 30  # 同城两场之间的最小间隔
    cross_city_base_buffer_minutes: int = 120  # 跨城赶场基础缓冲
    cross_city_buffer_per_km: float = 1.5  # 跨城每公里追加缓冲分钟
    unknown_distance_km: float = 1500.0  # 城市未知时的保守距离
    weights: ScoringWeights = field(default_factory=ScoringWeights)


def _coerce_enum(cls, value):
    """允许用枚举成员、成员名或中文值来指定枚举。"""
    if isinstance(value, cls):
        return value
    if isinstance(value, str):
        try:
            return cls[value]
        except KeyError:
            pass
    return cls(value)


class AssignmentService:
    """裁判指派平台入口。所有方法都是接口层可直接调用的公开 API。"""

    def __init__(
        self,
        repo: Repository | None = None,
        config: PlatformConfig | None = None,
        clock=None,
        cities: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        self.repo = repo or Repository()
        self.config = config or PlatformConfig()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cities = dict(DEFAULT_CITY_COORDS)
        if cities:
            self._cities.update(cities)

    # ------------------------------------------------------------------
    # 基础维护
    # ------------------------------------------------------------------

    def health(self) -> dict[str, str]:
        return {"service": "assignment", "status": "ok"}

    def register_city(self, name: str, lat: float, lon: float) -> None:
        self._cities[name] = (lat, lon)

    def add_referee(
        self,
        *,
        name: str,
        level,
        events,
        regions,
        home_city: str,
        max_daily_minutes: int | None = None,
        referee_id: str | None = None,
    ) -> Referee:
        """注册裁判：等级、可执法项目、执法区域、常驻城市。"""
        referee = Referee(
            id=referee_id or self._new_id("ref"),
            name=name,
            level=_coerce_enum(RefereeLevel, level),
            events=frozenset(events),
            regions=frozenset(regions),
            home_city=home_city,
            max_daily_minutes=max_daily_minutes,
        )
        with self.repo.transaction():
            self.repo.referees[referee.id] = referee
        return referee

    def add_unavailability(
        self,
        referee_id: str,
        start: datetime,
        end: datetime,
        *,
        tz: str | None = None,
        reason: str = "",
    ) -> TimeWindow:
        """为裁判登记一段不可用时段（绝对区间，可跨天）。"""
        with self.repo.transaction():
            referee = self.repo.get_referee(referee_id)
            window = TimeWindow(ensure_utc(start, tz), ensure_utc(end, tz), reason)
            if window.end <= window.start:
                raise PlatformError("不可用时段的结束时间必须晚于开始时间")
            referee.unavailable.append(window)
            return window

    def add_daily_unavailability(
        self,
        referee_id: str,
        start_hhmm: str,
        end_hhmm: str,
        *,
        tz: str,
        days: list[date],
        reason: str = "",
    ) -> list[TimeWindow]:
        """按当地钟点登记每日不可用时段；结束不晚于开始则视为跨天。"""
        windows = []
        with self.repo.transaction():
            referee = self.repo.get_referee(referee_id)
            for day in days:
                start_utc, end_utc = daily_window(day, start_hhmm, end_hhmm, tz)
                window = TimeWindow(start_utc, end_utc, reason)
                referee.unavailable.append(window)
                windows.append(window)
        return windows

    def add_match(
        self,
        *,
        name: str,
        level,
        event: str,
        region: str,
        city: str,
        tz: str,
        start: datetime,
        end: datetime,
        teams,
        slots_required: int = 1,
        match_id: str | None = None,
    ) -> Match:
        """登记比赛。``start``/``end`` 为赛地本地时间（朴素时间按 ``tz`` 解释）。"""
        start_utc = ensure_utc(start, tz)
        end_utc = ensure_utc(end, tz)
        if end_utc <= start_utc:
            raise PlatformError("比赛结束时间必须晚于开始时间")
        if slots_required < 1:
            raise PlatformError("slots_required 至少为 1")
        match = Match(
            id=match_id or self._new_id("m"),
            name=name,
            level=_coerce_enum(MatchLevel, level),
            event=event,
            region=region,
            city=city,
            tz=tz,
            start_utc=start_utc,
            end_utc=end_utc,
            teams=tuple(teams),
            slots_required=slots_required,
        )
        with self.repo.transaction():
            self.repo.matches[match.id] = match
        return match

    def set_match_status(
        self, match_id: str, status, *, actor: str = "system", reason: str = ""
    ) -> Match:
        """推进比赛状态（如标记为已结束）；状态变化写入比赛历史。"""
        with self.repo.transaction():
            match = self.repo.get_match(match_id)
            new_status = _coerce_enum(MatchStatus, status)
            if match.status == new_status:
                return match
            match.status = new_status
            match.version += 1
            match.history.append(
                AssignmentEvent(
                    seq=self.repo.next_seq(),
                    at=self._now(),
                    actor=actor,
                    action="STATUS",
                    reason=reason,
                    detail=f"比赛状态变更为 {new_status.value}",
                )
            )
            return match

    # ------------------------------------------------------------------
    # 利益冲突申报与重新评估
    # ------------------------------------------------------------------

    def declare_conflict(
        self,
        referee_id: str,
        team_id: str,
        kind,
        note: str = "",
        *,
        actor: str = "system",
    ) -> dict:
        """登记裁判主动申报的利益冲突，并重新评估其未开赛场次。

        未开赛场次上的有效指派会被标记为 ``FLAGGED_CONFLICT`` 等待主任处理；
        已结束比赛的历史指派保持原样，列入 ``skipped_finished`` 仅供查阅。
        """
        with self.repo.transaction():
            self.repo.get_referee(referee_id)
            now = self._now()
            conflict = ConflictOfInterest(
                id=self._new_id("coi"),
                referee_id=referee_id,
                team_id=team_id,
                kind=_coerce_enum(ConflictKind, kind),
                note=note,
                declared_at=now,
            )
            self.repo.conflicts[conflict.id] = conflict

            reevaluated: list[dict] = []
            skipped_finished: list[dict] = []
            skipped_started: list[dict] = []
            for assignment in self.repo.assignments_of_referee(referee_id):
                if assignment.status not in BOOKING_STATUSES:
                    continue
                match = self.repo.matches[assignment.match_id]
                if team_id not in match.teams:
                    continue
                if match.status == MatchStatus.FINISHED:
                    skipped_finished.append(
                        {
                            "assignment_id": assignment.id,
                            "match_id": match.id,
                            "reason": "比赛已结束，历史指派不能被覆盖",
                        }
                    )
                    continue
                if match.status == MatchStatus.CANCELLED:
                    continue
                if now >= match.start_utc:
                    skipped_started.append(
                        {
                            "assignment_id": assignment.id,
                            "match_id": match.id,
                            "reason": "比赛已开赛，现场指派不再变更",
                        }
                    )
                    continue
                assignment.status = AssignmentStatus.FLAGGED_CONFLICT
                self._append_event(
                    assignment,
                    actor,
                    "CONFLICT_FLAGGED",
                    reason=note or f"与球队 {team_id} 存在{conflict.kind.value}",
                    detail=f"新增冲突申报 {conflict.id}，未开赛场次重新评估",
                )
                match.version += 1
                reevaluated.append(
                    {
                        "assignment_id": assignment.id,
                        "match_id": match.id,
                        "action": "FLAGGED_CONFLICT",
                        "explanation": self._conflict_explanation(conflict, match),
                    }
                )
            return {
                "conflict": conflict,
                "reevaluated": reevaluated,
                "skipped_finished": skipped_finished,
                "skipped_started": skipped_started,
            }

    def deactivate_conflict(self, conflict_id: str, *, actor: str = "system", reason: str = "") -> ConflictOfInterest:
        """撤销一条冲突申报（如误报）；已被标记的指派需主任另行复核恢复。"""
        with self.repo.transaction():
            conflict = self.repo.get_conflict(conflict_id)
            conflict.active = False
            return conflict

    # ------------------------------------------------------------------
    # 候选名单与解释
    # ------------------------------------------------------------------

    def generate_candidates(
        self,
        match_id: str,
        *,
        limit: int | None = None,
        exclude_referee_ids=frozenset(),
    ) -> CandidateReport:
        """按比赛级别、距离、连续工作时长、近期场次生成候选名单。"""
        with self.repo.transaction():
            match = self.repo.get_match(match_id)
            return self._generate_candidates_locked(
                match, limit=limit, exclude_referee_ids=set(exclude_referee_ids)
            )

    def explain(self, match_id: str, referee_id: str) -> dict:
        """冲突/资格解释接口：说明某裁判对某场比赛的全部排除原因。"""
        with self.repo.transaction():
            match = self.repo.get_match(match_id)
            referee = self.repo.get_referee(referee_id)
            # 裁判在该场自身的指派不算“重复占用”，只解释真正的拦路规则。
            exclusions = self._check_eligibility(match, referee, exclude_match_id=match.id)
            reasons = []
            for ex in exclusions:
                item = {"code": ex.code, "category": ex.category, "detail": ex.detail}
                if ex.conflict_id:
                    conflict = self.repo.conflicts[ex.conflict_id]
                    item["conflict"] = {
                        "id": conflict.id,
                        "kind": conflict.kind.value,
                        "team_id": conflict.team_id,
                        "note": conflict.note,
                        "declared_at": conflict.declared_at.isoformat(),
                    }
                reasons.append(item)
            eligible = not exclusions
            if eligible:
                summary = f"{referee.name} 可以执法「{match.name}」"
            else:
                summary = "；".join(ex.detail for ex in exclusions)
            return {
                "match_id": match.id,
                "referee_id": referee.id,
                "eligible": eligible,
                "reasons": reasons,
                "summary": summary,
            }

    # ------------------------------------------------------------------
    # 指派流程：锁定 → 确认 → 接受/拒绝 → （撤销/替补）
    # ------------------------------------------------------------------

    def lock_candidates(self, match_id: str, referee_ids: list[str], *, director: str) -> dict:
        """主任在确认前锁定候选。返回 ``lock_token``（即比赛版本号）供确认使用。"""
        with self.repo.transaction():
            match = self.repo.get_match(match_id)
            self._require_match_open(match)
            if len(set(referee_ids)) != len(referee_ids):
                raise InvalidStateError("同一裁判不能占用同一场比赛的多个席位")
            # 重新锁定时旧 LOCKED 席位会被替换，计算剩余席位先将其扣除。
            locked_count = sum(
                1
                for a in self.repo.assignments_of_match(match.id)
                if a.status == AssignmentStatus.LOCKED
            )
            remaining = (
                match.slots_required - self._active_slot_count(match) + locked_count
            )
            if len(referee_ids) > remaining:
                raise InvalidStateError(
                    f"剩余席位 {remaining} 个，无法锁定 {len(referee_ids)} 名候选"
                )
            problems = self._eligibility_problems(match, referee_ids)
            if problems:
                raise NotEligibleError("部分候选不满足资格或冲突规则", details=problems)
            # 全部校验通过后再释放上一轮未确认的锁定（时间线保留 UNLOCK 事件）。
            for old in self.repo.assignments_of_match(match.id):
                if old.status == AssignmentStatus.LOCKED:
                    old.status = AssignmentStatus.REVOKED
                    self._append_event(old, director, "UNLOCK", reason="主任重新锁定候选")

            assignments = []
            slot = self._next_slot(match)
            for referee_id in referee_ids:
                assignment = self._create_assignment(match, referee_id, slot)
                slot += 1
                self._append_event(assignment, director, "LOCKED", reason="主任锁定候选")
                assignments.append(assignment)
            match.version += 1
            return {"lock_token": match.version, "assignments": assignments}

    def confirm(self, match_id: str, *, expected_version: int, director: str) -> list[Assignment]:
        """确认已锁定的候选。

        必须携带锁定时的版本号：并发确认或期间发生变更（如新增冲突申报）
        都会使版本号失效，从而保证只有一份确认生效、结果可追溯。
        """
        with self.repo.transaction():
            match = self.repo.get_match(match_id)
            self._require_match_open(match)
            if match.version != expected_version:
                raise VersionConflictError(
                    f"比赛版本已变化（期望 {expected_version}，当前 {match.version}），请重新锁定后再确认",
                    details={"expected": expected_version, "actual": match.version},
                )
            locked = [
                a
                for a in self.repo.assignments_of_match(match.id)
                if a.status == AssignmentStatus.LOCKED
            ]
            if not locked:
                raise InvalidStateError("没有待确认的锁定候选")
            # 锁定之后可能出现新的冲突申报或档期占用，确认前重新校验。
            problems = self._eligibility_problems(
                match, [a.referee_id for a in locked]
            )
            if problems:
                raise NotEligibleError("锁定候选在确认前已不再合格", details=problems)
            for assignment in locked:
                assignment.status = AssignmentStatus.CONFIRMED
                self._append_event(assignment, director, "CONFIRMED", reason="主任确认指派")
            match.version += 1
            return locked

    def respond(
        self,
        assignment_id: str,
        *,
        referee_id: str,
        accept: bool,
        reason: str,
    ) -> Assignment:
        """裁判接受或拒绝指派，必须填写理由，时间线保留记录。"""
        with self.repo.transaction():
            assignment = self.repo.get_assignment(assignment_id)
            match = self.repo.get_match(assignment.match_id)
            self._require_match_open(match)
            if assignment.referee_id != referee_id:
                raise InvalidStateError("只能由被指派的裁判本人响应")
            if not reason or not reason.strip():
                raise ReasonRequiredError("接受或拒绝都必须填写理由")
            if assignment.status != AssignmentStatus.CONFIRMED:
                raise InvalidStateError(
                    f"当前状态为 {assignment.status.value}，仅已确认的指派可以响应"
                )
            if accept:
                assignment.status = AssignmentStatus.ACCEPTED
                self._append_event(assignment, referee_id, "ACCEPTED", reason=reason)
            else:
                assignment.status = AssignmentStatus.DECLINED
                self._append_event(assignment, referee_id, "DECLINED", reason=reason)
            match.version += 1
            return assignment

    def revoke(self, assignment_id: str, *, director: str, reason: str) -> Assignment:
        """主任撤销未开赛场次的指派；已结束比赛的历史指派不能被覆盖。"""
        with self.repo.transaction():
            assignment = self.repo.get_assignment(assignment_id)
            match = self.repo.get_match(assignment.match_id)
            self._require_match_open(match)
            if not reason or not reason.strip():
                raise ReasonRequiredError("撤销指派必须填写理由")
            if assignment.status not in BOOKING_STATUSES:
                raise InvalidStateError(
                    f"当前状态为 {assignment.status.value}，无法撤销"
                )
            assignment.status = AssignmentStatus.REVOKED
            self._append_event(assignment, director, "REVOKED", reason=reason)
            match.version += 1
            return assignment

    def reinstate(self, assignment_id: str, *, director: str, reason: str) -> Assignment:
        """把被冲突标记的指派恢复为已确认（冲突解除或确认无误后）。"""
        with self.repo.transaction():
            assignment = self.repo.get_assignment(assignment_id)
            match = self.repo.get_match(assignment.match_id)
            self._require_match_open(match)
            if not reason or not reason.strip():
                raise ReasonRequiredError("恢复指派必须填写理由")
            if assignment.status != AssignmentStatus.FLAGGED_CONFLICT:
                raise InvalidStateError("仅冲突待复核的指派可以恢复")
            referee = self.repo.get_referee(assignment.referee_id)
            problems = self._eligibility_problems(match, [referee.id])
            if problems:
                raise NotEligibleError("该裁判仍不满足资格或冲突规则", details=problems)
            assignment.status = AssignmentStatus.CONFIRMED
            self._append_event(assignment, director, "REINSTATED", reason=reason)
            match.version += 1
            return assignment

    def substitute(
        self,
        assignment_id: str,
        *,
        director: str,
        reason: str,
        preferred_referee_id: str | None = None,
    ) -> dict:
        """替补接管：为被拒绝/撤销/冲突标记的指派寻找接替者。

        原指派转为 ``SUPERSEDED``，新指派直接确认为 ``CONFIRMED`` 并记录
        ``TAKEOVER`` 事件；找不到接替者时返回区分后的 ``empty_reason``。
        """
        with self.repo.transaction():
            old = self.repo.get_assignment(assignment_id)
            match = self.repo.get_match(old.match_id)
            self._require_match_open(match)
            if not reason or not reason.strip():
                raise ReasonRequiredError("替补接管必须填写理由")
            if old.status not in SUBSTITUTABLE_STATUSES:
                raise InvalidStateError(
                    f"当前状态为 {old.status.value}，仅被拒绝/已撤销/冲突待复核的指派可替补接管"
                )
            report = self._generate_candidates_locked(
                match, exclude_referee_ids={old.referee_id}
            )
            chosen_id = preferred_referee_id
            if chosen_id is not None:
                eligible_ids = {c.referee_id for c in report.candidates}
                if chosen_id not in eligible_ids:
                    problems = self._eligibility_problems(match, [chosen_id])
                    raise NotEligibleError("指定的替补人选不合格", details=problems)
            elif report.candidates:
                chosen_id = report.candidates[0].referee_id
            else:
                return {
                    "substituted": False,
                    "empty_reason": report.empty_reason,
                    "excluded": [e.to_dict() for e in report.excluded],
                }

            old.status = AssignmentStatus.SUPERSEDED
            self._append_event(old, director, "SUPERSEDED", reason=reason)
            new = self._create_assignment(match, chosen_id, old.slot)
            new.status = AssignmentStatus.CONFIRMED
            self._append_event(
                new,
                director,
                "TAKEOVER",
                reason=reason,
                detail=f"接替 {old.referee_id}（原指派 {old.id}）",
            )
            match.version += 1
            return {
                "substituted": True,
                "assignment": new,
                "replaced_referee_id": old.referee_id,
                "report": report,
            }

    def batch_assign(self, match_ids: list[str], *, director: str) -> dict:
        """批量指派：按开赛时间与比赛级别依次自动补足各场席位。

        每场比赛独立生成候选并立即确认；同一批内先确认的场次会占用裁判
        档期，后续场次自动避开，保证不会重复占用同一裁判。
        """
        with self.repo.transaction():
            matches = [self.repo.get_match(mid) for mid in match_ids]
            matches.sort(key=lambda m: (m.start_utc, -int(m.level), m.id))
            results = []
            for match in matches:
                if match.status != MatchStatus.SCHEDULED:
                    results.append(
                        {
                            "match_id": match.id,
                            "status": "SKIPPED",
                            "reason": f"比赛状态为 {match.status.value}，不参与批量指派",
                        }
                    )
                    continue
                needed = match.slots_required - self._active_slot_count(match)
                if needed <= 0:
                    results.append(
                        {"match_id": match.id, "status": "FULL", "assigned": []}
                    )
                    continue
                report = self._generate_candidates_locked(match, limit=needed)
                chosen = report.candidates[:needed]
                assigned = []
                slot = self._next_slot(match)
                for candidate in chosen:
                    assignment = self._create_assignment(
                        match, candidate.referee_id, slot
                    )
                    slot += 1
                    assignment.status = AssignmentStatus.CONFIRMED
                    self._append_event(
                        assignment, director, "CONFIRMED", reason="批量指派"
                    )
                    assigned.append(assignment)
                if assigned:
                    match.version += 1
                results.append(
                    {
                        "match_id": match.id,
                        "status": "ASSIGNED" if assigned else "UNSTAFFED",
                        "assigned": [a.id for a in assigned],
                        "referee_ids": [a.referee_id for a in assigned],
                        "empty_reason": None if assigned else report.empty_reason,
                        "excluded": []
                        if assigned
                        else [e.to_dict() for e in report.excluded],
                    }
                )
            return {"results": results}

    # ------------------------------------------------------------------
    # 查询与导出
    # ------------------------------------------------------------------

    def timeline(self, assignment_id: str) -> list[AssignmentEvent]:
        """某次指派的完整时间线（按全局序号排序）。"""
        with self.repo.transaction():
            assignment = self.repo.get_assignment(assignment_id)
            return sorted(assignment.timeline, key=lambda e: e.seq)

    def match_assignments(self, match_id: str) -> list[Assignment]:
        with self.repo.transaction():
            self.repo.get_match(match_id)
            return self.repo.assignments_of_match(match_id)

    def export_by_date(self, day, tz: str, *, fmt: str = "json"):
        """按日期导出指派。日期按 ``tz`` 指定的时区解释。

        ``fmt="json"`` 返回结构化字典；``fmt="csv"`` 返回 CSV 文本。
        已结束比赛的历史指派照常导出，保证记录可追溯。
        """
        if isinstance(day, str):
            day = date.fromisoformat(day)
        zone = ZoneInfo(tz)
        with self.repo.transaction():
            rows = []
            for match in sorted(
                self.repo.matches.values(), key=lambda m: (m.start_utc, m.id)
            ):
                if match.start_utc.astimezone(zone).date() != day:
                    continue
                assignments = sorted(
                    self.repo.assignments_of_match(match.id), key=lambda a: a.slot
                )
                if not assignments:
                    rows.append(self._export_row(match, None, zone))
                for assignment in assignments:
                    rows.append(self._export_row(match, assignment, zone))
            if fmt == "csv":
                return self._rows_to_csv(rows)
            if fmt != "json":
                raise PlatformError(f"不支持的导出格式: {fmt}")
            return {"date": day.isoformat(), "tz": tz, "rows": rows}

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        return self._clock()

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"

    def _append_event(
        self, assignment: Assignment, actor: str, action: str, reason: str = "", detail: str = ""
    ) -> None:
        assignment.timeline.append(
            AssignmentEvent(
                seq=self.repo.next_seq(),
                at=self._now(),
                actor=actor,
                action=action,
                reason=reason,
                detail=detail,
            )
        )

    def _create_assignment(self, match: Match, referee_id: str, slot: int) -> Assignment:
        assignment = Assignment(
            id=self._new_id("asg"),
            match_id=match.id,
            referee_id=referee_id,
            slot=slot,
            status=AssignmentStatus.LOCKED,
            created_at=self._now(),
        )
        self.repo.assignments[assignment.id] = assignment
        return assignment

    def _require_match_open(self, match: Match) -> None:
        if match.status == MatchStatus.FINISHED:
            raise MatchFinishedError(
                f"比赛「{match.name}」已结束，历史指派不能被覆盖"
            )
        if match.status == MatchStatus.CANCELLED:
            raise InvalidStateError(f"比赛「{match.name}」已取消")

    def _active_slot_count(self, match: Match) -> int:
        return sum(
            1
            for a in self.repo.assignments_of_match(match.id)
            if a.status in BOOKING_STATUSES
        )

    def _next_slot(self, match: Match) -> int:
        slots = [a.slot for a in self.repo.assignments_of_match(match.id)]
        return (max(slots) + 1) if slots else 1

    def _eligibility_problems(self, match: Match, referee_ids) -> dict:
        problems = {}
        for referee_id in referee_ids:
            referee = self.repo.get_referee(referee_id)
            exclusions = self._check_eligibility(
                match, referee, exclude_match_id=match.id
            )
            if exclusions:
                problems[referee_id] = [e.to_dict() for e in exclusions]
        return problems

    def _generate_candidates_locked(
        self,
        match: Match,
        *,
        limit: int | None = None,
        exclude_referee_ids: set[str] | None = None,
    ) -> CandidateReport:
        exclude_referee_ids = exclude_referee_ids or set()
        candidates: list[Candidate] = []
        excluded: list[Exclusion] = []
        for referee in self.repo.referees.values():
            if referee.id in exclude_referee_ids:
                continue
            exclusions = self._check_eligibility(match, referee)
            if exclusions:
                excluded.extend(exclusions)
            else:
                candidates.append(self._score(match, referee))
        candidates.sort(key=lambda c: (c.score, c.referee_id))
        if limit is not None:
            candidates = candidates[:limit]

        empty_reason = None
        if not candidates:
            blocked_by_other = {
                e.referee_id for e in excluded if e.category != "conflict"
            }
            blocked_by_conflict = {
                e.referee_id for e in excluded if e.category == "conflict"
            }
            if blocked_by_conflict - blocked_by_other:
                empty_reason = "ALL_EXCLUDED_BY_CONFLICT"
            else:
                empty_reason = "NO_QUALIFIED_CANDIDATES"
        return CandidateReport(
            match_id=match.id,
            candidates=candidates,
            excluded=excluded,
            empty_reason=empty_reason,
        )

    def _check_eligibility(
        self, match: Match, referee: Referee, *, exclude_match_id: str | None = None
    ) -> list[Exclusion]:
        """对一名裁判执行全部硬性规则，返回排除原因列表（空即合格）。"""
        exclusions: list[Exclusion] = []

        def add(code: str, category: str, detail: str, conflict_id: str | None = None):
            exclusions.append(
                Exclusion(
                    referee_id=referee.id,
                    referee_name=referee.name,
                    code=code,
                    category=category,
                    detail=detail,
                    conflict_id=conflict_id,
                )
            )

        # 1. 裁判等级须覆盖比赛级别
        required = self.config.min_level_by_match[match.level]
        if referee.level < required:
            add(
                "LEVEL_TOO_LOW",
                "qualification",
                f"「{match.name}」为{match.level.label}，要求{required.label}及以上，"
                f"{referee.name}为{referee.level.label}",
            )
        # 2. 可执法项目
        if match.event not in referee.events:
            add(
                "EVENT_NOT_COVERED",
                "qualification",
                f"{referee.name} 未登记可执法项目「{match.event}」",
            )
        # 3. 执法区域
        if match.region not in referee.regions:
            add(
                "REGION_NOT_COVERED",
                "qualification",
                f"{referee.name} 的执法区域不包含「{match.region}」",
            )
        # 4. 不可用时段（含跨天时段，均按 UTC 区间比较）
        for window in referee.unavailable:
            if overlaps(window.start, window.end, match.start_utc, match.end_utc):
                local = window.start.astimezone(ZoneInfo(match.tz))
                add(
                    "UNAVAILABLE",
                    "availability",
                    f"{referee.name} 申报的不可用时段（{local:%Y-%m-%d %H:%M} 起"
                    f"{f'，原因：{window.reason}' if window.reason else ''}）与比赛重叠",
                )
        # 5. 重复占用与跨城赶场
        for other_assignment, other_match in self._booked_matches(
            referee.id, exclude_match_id=exclude_match_id
        ):
            if overlaps(
                other_match.start_utc, other_match.end_utc,
                match.start_utc, match.end_utc,
            ):
                add(
                    "ALREADY_BOOKED",
                    "availability",
                    f"{referee.name} 在相同时段已承担「{other_match.name}」，不能重复占用",
                )
                continue
            gap = gap_minutes(
                other_match.start_utc, other_match.end_utc,
                match.start_utc, match.end_utc,
            )
            required_gap = self._required_gap_minutes(other_match.city, match.city)
            if gap < required_gap:
                add(
                    "INSUFFICIENT_TRAVEL_GAP",
                    "availability",
                    f"与「{other_match.name}」间隔仅 {gap:.0f} 分钟，"
                    f"{'同城' if other_match.city == match.city else '跨城'}至少需 {required_gap:.0f} 分钟",
                )
        # 6. 当日连续工作时长
        _, chain_span = self._day_stats(referee.id, match)
        limit = referee.max_daily_minutes or self.config.max_daily_minutes
        if chain_span > limit:
            add(
                "DAILY_LIMIT_EXCEEDED",
                "availability",
                f"{referee.name} 当日连续执法将达 {chain_span:.0f} 分钟，超过上限 {limit} 分钟",
            )
        # 7. 利益冲突（主动申报）
        for conflict in self.repo.active_conflicts_of(referee.id):
            if conflict.team_id in match.teams:
                add(
                    "CONFLICT_OF_INTEREST",
                    "conflict",
                    self._conflict_explanation(conflict, match),
                    conflict_id=conflict.id,
                )
        return exclusions

    def _conflict_explanation(self, conflict: ConflictOfInterest, match: Match) -> str:
        referee = self.repo.referees.get(conflict.referee_id)
        name = referee.name if referee else conflict.referee_id
        note = f"（{conflict.note}）" if conflict.note else ""
        return (
            f"{name} 申报了与球队 {conflict.team_id} 的{conflict.kind.value}{note}，"
            f"按冲突规则不得执法「{match.name}」"
        )

    def _booked_matches(self, referee_id: str, *, exclude_match_id: str | None = None):
        """裁判当前仍占用档期的 (assignment, match) 列表。"""
        result = []
        for assignment in self.repo.assignments_of_referee(referee_id):
            if assignment.status not in BOOKING_STATUSES:
                continue
            if exclude_match_id and assignment.match_id == exclude_match_id:
                continue
            match = self.repo.matches[assignment.match_id]
            if match.status == MatchStatus.CANCELLED:
                continue
            result.append((assignment, match))
        return result

    def _required_gap_minutes(self, city_a: str, city_b: str) -> float:
        if city_a == city_b:
            return float(self.config.same_city_buffer_minutes)
        distance = self._distance_km(city_a, city_b)
        if distance is None:
            return self.config.cross_city_base_buffer_minutes * 2.0
        return max(
            float(self.config.cross_city_base_buffer_minutes),
            distance * self.config.cross_city_buffer_per_km,
        )

    def _distance_km(self, city_a: str, city_b: str) -> float | None:
        a = self._cities.get(city_a)
        b = self._cities.get(city_b)
        if a is None or b is None:
            return None
        return haversine_km(a[0], a[1], b[0], b[1])

    def _day_stats(self, referee_id: str, match: Match) -> tuple[float, float]:
        """裁判在比赛日（赛地时区）的总执法分钟与含本场在内的连续工作跨度。"""
        zone = ZoneInfo(match.tz)
        day = match.start_utc.astimezone(zone).date()
        windows = []
        for _, other in self._booked_matches(referee_id, exclude_match_id=match.id):
            if other.start_utc.astimezone(zone).date() == day:
                windows.append((other.start_utc, other.end_utc))
        total = sum((end - start).total_seconds() / 60.0 for start, end in windows)

        # 与候选场次间隔不超过 chain_gap_minutes 的场次串联为一个连续块。
        chain = [(match.start_utc, match.end_utc)]
        pool = list(windows)
        changed = True
        while changed:
            changed = False
            for window in list(pool):
                for c_start, c_end in chain:
                    if gap_minutes(window[0], window[1], c_start, c_end) <= self.config.chain_gap_minutes:
                        chain.append(window)
                        pool.remove(window)
                        changed = True
                        break
        chain_start = min(start for start, _ in chain)
        chain_end = max(end for _, end in chain)
        span = (chain_end - chain_start).total_seconds() / 60.0
        return total, span

    def _score(self, match: Match, referee: Referee) -> Candidate:
        distance = self._distance_km(referee.home_city, match.city)
        if distance is None:
            distance = self.config.unknown_distance_km
        window_start = match.start_utc.timestamp() - self.config.recent_window_days * 86400
        recent = 0
        for _, other in self._booked_matches(referee.id, exclude_match_id=match.id):
            if window_start <= other.start_utc.timestamp() <= match.start_utc.timestamp():
                recent += 1
        day_total, _ = self._day_stats(referee.id, match)
        weights = self.config.weights
        score = (
            weights.distance_km * distance
            + weights.recent_match * recent
            + weights.day_minute * day_total
        )
        return Candidate(
            referee_id=referee.id,
            referee_name=referee.name,
            score=round(score, 3),
            distance_km=round(distance, 1),
            recent_matches=recent,
            day_minutes=round(day_total, 1),
            breakdown={
                "distance_km": round(distance, 1),
                "recent_matches": recent,
                "day_minutes": round(day_total, 1),
                "weights": {
                    "distance_km": weights.distance_km,
                    "recent_match": weights.recent_match,
                    "day_minute": weights.day_minute,
                },
            },
        )

    def _export_row(self, match: Match, assignment: Assignment | None, zone) -> dict:
        row = {
            "date": match.start_utc.astimezone(zone).date().isoformat(),
            "match_id": match.id,
            "match_name": match.name,
            "match_level": match.level.label,
            "event": match.event,
            "region": match.region,
            "city": match.city,
            "start_local": match.start_utc.astimezone(zone).isoformat(),
            "end_local": match.end_utc.astimezone(zone).isoformat(),
            "match_status": match.status.value,
            "slot": "",
            "assignment_id": "",
            "referee_id": "",
            "referee_name": "",
            "assignment_status": "UNASSIGNED",
            "last_event": "",
            "last_event_reason": "",
        }
        if assignment is not None:
            referee = self.repo.referees.get(assignment.referee_id)
            last = max(assignment.timeline, key=lambda e: e.seq) if assignment.timeline else None
            row.update(
                {
                    "slot": assignment.slot,
                    "assignment_id": assignment.id,
                    "referee_id": assignment.referee_id,
                    "referee_name": referee.name if referee else "",
                    "assignment_status": assignment.status.value,
                    "last_event": last.action if last else "",
                    "last_event_reason": last.reason if last else "",
                }
            )
        return row

    @staticmethod
    def _rows_to_csv(rows: list[dict]) -> str:
        buffer = io.StringIO()
        if not rows:
            return ""
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue()
