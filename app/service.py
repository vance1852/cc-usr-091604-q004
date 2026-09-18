"""应用服务：线程安全的裁判指派操作边界。

所有变更都在 ``Store`` 的全局锁内完成；确认类操作带 lock_version 乐观并发控制。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from .eligibility import (
    REASON_ALREADY_ON_MATCH,
    Reason,
    evaluate_official,
    generate_candidates,
)
from .models import (
    Assignment,
    AssignmentStatus,
    ConflictDeclaration,
    ConflictType,
    Grade,
    Match,
    Official,
    Team,
    Unavailability,
    UnavailabilityKind,
)
from .policy import Policy
from .store import Store
from .timeutils import local_date_of, now_utc, parse_dt


class AssignmentError(Exception):
    """业务错误基类，携带机器可读 code。"""

    code = "assignment_error"

    def __init__(self, message: str, *, reasons: Optional[list[Reason]] = None):
        super().__init__(message)
        self.reasons = reasons or []


class NotFoundError(AssignmentError):
    code = "not_found"


class EligibilityError(AssignmentError):
    """硬性资格不满足（未找到合格人选侧的原因）。"""

    code = "not_qualified"


class RuleExclusionError(AssignmentError):
    """资格合格但被冲突/排班规则排除。"""

    code = "excluded_by_rules"


class StateConflictError(AssignmentError):
    """并发确认或状态流转冲突。"""

    code = "state_conflict"


class HistoricalAssignmentProtected(AssignmentError):
    """已结束比赛的历史指派不可覆盖。"""

    code = "historical_protected"


class AssignmentService:
    def __init__(
        self,
        store: Optional[Store] = None,
        policy: Optional[Policy] = None,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.store = store or Store()
        self.policy = policy or Policy()
        self._clock = clock

    # ---------- 基础档案 ----------

    def create_official(
        self,
        official_id: str,
        name: str,
        grade: str,
        home_city: str,
        sports: Optional[set[str]] = None,
        region_cities: Optional[set[str]] = None,
    ) -> Official:
        with self.store.lock():
            if official_id in self.store.officials:
                raise StateConflictError(f"裁判 {official_id} 已存在")
            official = Official(
                id=official_id,
                name=name,
                grade=Grade.of(grade),
                home_city=home_city,
                sports=frozenset(sports or {"篮球"}),
                region_cities=frozenset(region_cities or set()),
            )
            self.store.officials[official_id] = official
            return official

    def set_official_active(self, official_id: str, active: bool) -> Official:
        with self.store.lock():
            official = self._require_official(official_id)
            official.active = active
            return official

    def create_team(self, team_id: str, name: str, city: str) -> Team:
        with self.store.lock():
            if team_id in self.store.teams:
                raise StateConflictError(f"队伍 {team_id} 已存在")
            team = Team(id=team_id, name=name, city=city)
            self.store.teams[team_id] = team
            return team

    def create_match(
        self,
        match_id: str,
        level: str,
        sport: str,
        home_team_id: str,
        away_team_id: str,
        city: str,
        venue: str,
        start: str | datetime,
        end: str | datetime,
        tz_name: str = "Asia/Shanghai",
    ) -> Match:
        with self.store.lock():
            from .models import MATCH_MIN_GRADE

            if level not in MATCH_MIN_GRADE:
                raise NotFoundError(f"未知比赛级别：{level}")
            for tid in (home_team_id, away_team_id):
                if tid not in self.store.teams:
                    raise NotFoundError(f"队伍 {tid} 不存在")
            start_dt = parse_dt(start, tz_name)
            end_dt = parse_dt(end, tz_name)
            if end_dt <= start_dt:
                raise AssignmentError("比赛结束时间必须晚于开始时间")
            match = Match(
                id=match_id,
                level=level,
                sport=sport,
                home_team_id=home_team_id,
                away_team_id=away_team_id,
                city=city,
                venue=venue,
                start=start_dt,
                end=end_dt,
                tz_name=tz_name,
            )
            self.store.matches[match_id] = match
            return match

    def mark_started(self, match_id: str, at: Optional[datetime] = None) -> Match:
        with self.store.lock():
            match = self._require_match(match_id)
            match.started_at = at or self._clock()
            return match

    def mark_finished(self, match_id: str, at: Optional[datetime] = None) -> Match:
        with self.store.lock():
            match = self._require_match(match_id)
            if match.started_at is None:
                match.started_at = match.start
            match.finished_at = at or self._clock()
            return match

    # ---------- 不可用时段与利益冲突 ----------

    def add_unavailability(
        self,
        official_id: str,
        start: str | datetime,
        end: str | datetime,
        reason: str,
        tz_name: str = "Asia/Shanghai",
        kind: str = UnavailabilityKind.PERSONAL.value,
    ) -> dict:
        with self.store.lock():
            official = self._require_official(official_id)
            start_dt = parse_dt(start, tz_name)
            end_dt = parse_dt(end, tz_name)
            if end_dt <= start_dt:
                raise AssignmentError("不可用时段结束时间必须晚于开始时间")
            declaration = Unavailability(
                id=self.store.next_id("una"),
                official_id=official_id,
                start=start_dt,
                end=end_dt,
                reason=reason,
                kind=UnavailabilityKind(kind),
            )
            self.store.unavailabilities.append(declaration)
            affected = self._reevaluate_upcoming(
                official, trigger="unavailability_added", trigger_id=declaration.id
            )
            return {
                "unavailability": {
                    "id": declaration.id,
                    "official_id": official_id,
                    "from": start_dt.isoformat(),
                    "to": end_dt.isoformat(),
                    "reason": reason,
                },
                "reevaluated": affected,
            }

    def declare_conflict(
        self,
        official_id: str,
        team_id: str,
        conflict_type: str,
        detail: str = "",
    ) -> dict:
        """新增利益冲突申报，并立即重评估该裁判所有未开赛场次。

        已结束比赛的历史指派永不触碰。
        """
        with self.store.lock():
            official = self._require_official(official_id)
            if team_id not in self.store.teams:
                raise NotFoundError(f"队伍 {team_id} 不存在")
            ctype = ConflictType(conflict_type)
            # 同一裁判+队伍+类型的有效申报不重复登记
            for c in self.store.conflicts:
                if (
                    c.active
                    and c.official_id == official_id
                    and c.team_id == team_id
                    and c.conflict_type == ctype
                ):
                    raise StateConflictError("该利益冲突已申报且仍有效")
            declaration = ConflictDeclaration(
                id=self.store.next_id("cfl"),
                official_id=official_id,
                team_id=team_id,
                conflict_type=ctype,
                detail=detail,
                declared_at=self._clock(),
            )
            self.store.conflicts.append(declaration)
            affected = self._reevaluate_upcoming(
                official, trigger="conflict_declared", trigger_id=declaration.id
            )
            return {
                "declaration": {
                    "id": declaration.id,
                    "official_id": official_id,
                    "team_id": team_id,
                    "conflict_type": ctype.value,
                    "detail": detail,
                    "declared_at": declaration.declared_at.isoformat(),
                },
                "reevaluated": affected,
            }

    def _reevaluate_upcoming(self, official: Official, trigger: str, trigger_id: str) -> dict:
        """对未开赛场次上占用中的指派重新评估；不合格即作废（superseded）。

        已开赛（含进行中）与已结束比赛的指派都受保护，绝不覆盖。
        """
        superseded: list[str] = []
        matches_open: list[str] = []
        protected_started: list[str] = []
        for assignment in list(self.store.assignments.values()):
            if assignment.official_id != official.id or not assignment.status.occupies:
                continue
            match = self.store.matches.get(assignment.match_id)
            if match is None:
                continue
            if match.is_started:
                # 已开赛/已结束的历史指派受保护
                protected_started.append(match.id)
                continue
            matches_open.append(match.id)
            verdict = evaluate_official(
                self.store,
                official,
                match,
                self.policy,
                ignore_assignment_id=assignment.id,
            )
            if not verdict.eligible:
                reason_codes = [r.code for r in verdict.exclusions]
                assignment.status = AssignmentStatus.SUPERSEDED
                assignment.lock_version += 1
                assignment.add_event(
                    actor="system",
                    action=f"reevaluate:{trigger}",
                    reason="; ".join(r.message for r in verdict.exclusions),
                    trigger_id=trigger_id,
                    reason_codes=reason_codes,
                )
                superseded.append(assignment.id)
        return {
            "upcoming_match_ids": sorted(set(matches_open)),
            "superseded_assignment_ids": superseded,
            "started_or_finished_protected": sorted(set(protected_started)),
            "finished_matches_protected": sorted(
                {
                    mid
                    for mid in set(protected_started)
                    if self.store.matches[mid].is_finished
                }
            ),
        }

    # ---------- 候选名单 ----------

    def candidates(self, match_id: str, limit: Optional[int] = None) -> dict:
        with self.store.lock():
            match = self._require_match(match_id)
            return generate_candidates(self.store, match, self.policy, limit=limit)

    def explain(self, match_id: str, official_id: str) -> dict:
        from .eligibility import explain_conflicts

        with self.store.lock():
            match = self._require_match(match_id)
            official = self._require_official(official_id)
            return explain_conflicts(self.store, match, official)

    # ---------- 锁定 / 确认 / 响应 ----------

    def _occupying_for_match_official(self, match_id: str, official_id: str) -> Optional[Assignment]:
        for a in self.store.assignments.values():
            if (
                a.match_id == match_id
                and a.official_id == official_id
                and a.status.occupies
            ):
                return a
        return None

    def lock_candidate(self, match_id: str, official_id: str, actor: str) -> Assignment:
        """主任确认前锁定候选。锁定瞬间重新跑规则，防止期间新增冲突/占用。"""
        with self.store.lock():
            match = self._require_match(match_id)
            self._ensure_open(match)
            official = self._require_official(official_id)

            existing = self._occupying_for_match_official(match_id, official_id)
            if existing is not None:
                raise StateConflictError(
                    f"裁判 {official.name} 在比赛 {match_id} 上已有占用中的指派 {existing.id}",
                    reasons=[
                        Reason(
                            REASON_ALREADY_ON_MATCH,
                            "该裁判已在该场次上被锁定/确认",
                            {"assignment_id": existing.id, "status": existing.status.value},
                        )
                    ],
                )

            verdict = evaluate_official(
                self.store, official, match, self.policy, ignore_match_id=match.id
            )
            if verdict.disqualified:
                raise EligibilityError(
                    f"{official.name} 不具备该场次的硬性资格", reasons=verdict.disqualified
                )
            if verdict.exclusions:
                raise RuleExclusionError(
                    f"{official.name} 被规则排除，无法锁定", reasons=verdict.exclusions
                )

            assignment = Assignment(
                id=self.store.next_id("asn"),
                match_id=match_id,
                official_id=official_id,
                status=AssignmentStatus.LOCKED,
                created_at=self._clock(),
            )
            assignment.add_event(actor=actor, action="locked")
            self.store.assignments[assignment.id] = assignment
            return assignment

    def confirm_assignment(
        self,
        assignment_id: str,
        actor: str,
        expected_version: Optional[int] = None,
    ) -> Assignment:
        """确认指派。CAS：仅 LOCKED 且版本号匹配时成功，用于并发确认。"""
        with self.store.lock():
            assignment = self._require_assignment(assignment_id)
            match = self._require_match(assignment.match_id)
            self._ensure_open(match)

            if expected_version is not None and assignment.lock_version != expected_version:
                raise StateConflictError(
                    f"指派 {assignment_id} 版本已变化：期望 {expected_version}，"
                    f"当前 {assignment.lock_version}",
                )
            if assignment.status != AssignmentStatus.LOCKED:
                raise StateConflictError(
                    f"指派 {assignment_id} 当前状态为 {assignment.status.value}，不能确认"
                )
            # 确认前再次校验规则（锁定后可能有新申报/新占用），忽略指派自身
            official = self._require_official(assignment.official_id)
            verdict = evaluate_official(
                self.store,
                official,
                match,
                self.policy,
                ignore_assignment_id=assignment.id,
            )
            if not verdict.eligible:
                reasons = verdict.disqualified or verdict.exclusions
                raise RuleExclusionError(
                    "确认时复核未通过，候选已不再可指派", reasons=reasons
                )

            assignment.status = AssignmentStatus.CONFIRMED
            assignment.confirmed_by = actor
            assignment.confirmed_at = self._clock()
            assignment.lock_version += 1
            assignment.add_event(
                actor=actor,
                action="confirmed",
                by=actor,
                at=assignment.confirmed_at.isoformat(),
            )
            return assignment

    def respond(
        self, assignment_id: str, accept: bool, reason: str, actor: Optional[str] = None
    ) -> Assignment:
        """裁判接受或拒绝，必须带理由；全过程写入时间线。"""
        reason = (reason or "").strip()
        if not reason:
            raise AssignmentError("接受或拒绝都必须填写理由")
        with self.store.lock():
            assignment = self._require_assignment(assignment_id)
            if assignment.status != AssignmentStatus.CONFIRMED:
                raise StateConflictError(
                    f"指派 {assignment_id} 当前状态为 {assignment.status.value}，裁判无法响应"
                )
            match = self._require_match(assignment.match_id)
            self._ensure_open(match)
            actor = actor or assignment.official_id
            assignment.status = (
                AssignmentStatus.ACCEPTED if accept else AssignmentStatus.DECLINED
            )
            assignment.response_reason = reason
            assignment.responded_at = self._clock()
            assignment.lock_version += 1
            assignment.add_event(
                actor=actor,
                action="accepted" if accept else "declined",
                reason=reason,
                at=assignment.responded_at.isoformat(),
            )
            return assignment

    def cancel_assignment(self, assignment_id: str, reason: str, actor: str) -> Assignment:
        reason = (reason or "").strip()
        if not reason:
            raise AssignmentError("撤销指派必须填写原因")
        with self.store.lock():
            assignment = self._require_assignment(assignment_id)
            match = self._require_match(assignment.match_id)
            self._ensure_open(match)
            if assignment.status not in (
                AssignmentStatus.LOCKED,
                AssignmentStatus.CONFIRMED,
                AssignmentStatus.ACCEPTED,
            ):
                raise StateConflictError(
                    f"指派 {assignment_id} 状态 {assignment.status.value} 不可撤销"
                )
            assignment.status = AssignmentStatus.CANCELLED
            assignment.cancelled_reason = reason
            assignment.lock_version += 1
            assignment.add_event(actor=actor, action="cancelled", reason=reason)
            return assignment

    # ---------- 批量指派 ----------

    def batch_assign(
        self, requests: list[dict], actor: str
    ) -> dict:
        """批量锁定+确认多场比赛的裁判。

        原子提交：任一项不满足规则则整批失败、不产生任何指派；
        同一裁判在批内被两场时间冲突的比赛申请时也会被拦截。
        """
        if not requests:
            raise AssignmentError("批量指派请求为空")
        with self.store.lock():
            planned: list[tuple[Match, Official, Assignment]] = []
            failures: list[dict] = []

            # 批内去重：同一场比赛不能重复指定同一裁判
            batch_keys: set[tuple[str, str]] = set()

            for idx, req in enumerate(requests):
                match_id = req["match_id"]
                official_id = req["official_id"]
                item = {"index": idx, "match_id": match_id, "official_id": official_id}
                try:
                    match = self._require_match(match_id)
                    self._ensure_open(match)
                    official = self._require_official(official_id)
                    key = (match_id, official_id)
                    if key in batch_keys:
                        raise StateConflictError("批内重复：同一场比赛指定了同一裁判")
                    batch_keys.add(key)

                    if self._occupying_for_match_official(match_id, official_id):
                        raise StateConflictError("该裁判在该场次已有占用中的指派")

                    verdict = evaluate_official(
                        self.store, official, match, self.policy,
                        ignore_match_id=match.id,
                    )
                    if verdict.disqualified:
                        raise EligibilityError(
                            f"{official.name} 不具备硬性资格", reasons=verdict.disqualified
                        )
                    if verdict.exclusions:
                        raise RuleExclusionError(
                            f"{official.name} 被规则排除", reasons=verdict.exclusions
                        )

                    # 与“库内已占用 + 批内已规划”的全部场次做排班检查，
                    # 确保跨城转场、连续工作、近期场次在批量口径下也不漏算
                    from .eligibility import (
                        _occupying_assignments,
                        _schedule_reasons,
                    )

                    others: list[tuple] = [
                        (a, self._require_match(a.match_id))
                        for a in _occupying_assignments(self.store, official.id)
                        if a.match_id != match.id
                    ]
                    others.extend(
                        (self._proxy_assignment(po), pm)
                        for pm, po, _ in planned
                        if po.id == official.id
                    )
                    clash = _schedule_reasons(
                        self.store, official, match, others, self.policy
                    )
                    if clash:
                        raise RuleExclusionError(
                            f"{official.name} 与既有/批内另一场比赛排班冲突",
                            reasons=clash,
                        )

                    assignment = Assignment(
                        id=self.store.next_id("asn"),
                        match_id=match_id,
                        official_id=official_id,
                        status=AssignmentStatus.LOCKED,
                        created_at=self._clock(),
                    )
                    planned.append((match, official, assignment))
                except AssignmentError as exc:
                    failures.append(
                        {
                            **item,
                            "error_code": exc.code,
                            "message": str(exc),
                            "reasons": [r.to_dict() for r in exc.reasons],
                        }
                    )

            if failures:
                return {"committed": False, "results": [], "failures": failures}

            results = []
            for match, official, assignment in planned:
                assignment.add_event(actor=actor, action="locked", batch=True)
                self.store.assignments[assignment.id] = assignment
            for match, official, assignment in planned:
                assignment.status = AssignmentStatus.CONFIRMED
                assignment.confirmed_by = actor
                assignment.confirmed_at = self._clock()
                assignment.lock_version += 1
                assignment.add_event(actor=actor, action="confirmed", batch=True)
                results.append(
                    {
                        "match_id": match.id,
                        "official_id": official.id,
                        "assignment_id": assignment.id,
                        "status": assignment.status.value,
                        "lock_version": assignment.lock_version,
                    }
                )
            return {"committed": True, "results": results, "failures": []}

    def _proxy_assignment(self, official: Official) -> Assignment:
        """批内冲突检查时代表“计划中但尚未落库”的指派。"""
        return Assignment(
            id="__planned__",
            match_id="",
            official_id=official.id,
            status=AssignmentStatus.CONFIRMED,
            created_at=self._clock(),
        )

    # ---------- 替补接管 ----------

    def substitute_takeover(
        self,
        match_id: str,
        new_official_id: str,
        reason: str,
        actor: str,
        old_assignment_id: Optional[str] = None,
    ) -> dict:
        """原裁判无法执法时由替补接管：旧指派保留时间线并标记 replaced。"""
        reason = (reason or "").strip()
        if not reason:
            raise AssignmentError("替补接管必须填写原因")
        with self.store.lock():
            match = self._require_match(match_id)
            self._ensure_open(match)

            if old_assignment_id is None:
                # 默认接管该场次上最近一条“待补”的指派：占用中或已被拒绝
                pool = [
                    a
                    for a in self.store.assignments.values()
                    if a.match_id == match_id
                    and a.status
                    in (
                        AssignmentStatus.LOCKED,
                        AssignmentStatus.CONFIRMED,
                        AssignmentStatus.ACCEPTED,
                        AssignmentStatus.DECLINED,
                    )
                ]
                if not pool:
                    raise NotFoundError(f"比赛 {match_id} 没有可接管的有效指派")
                old = max(pool, key=lambda a: a.created_at)
            else:
                old = self._require_assignment(old_assignment_id)
                if old.match_id != match_id:
                    raise AssignmentError("旧指派与比赛不匹配")
                if old.status not in (
                    AssignmentStatus.LOCKED,
                    AssignmentStatus.CONFIRMED,
                    AssignmentStatus.ACCEPTED,
                    AssignmentStatus.DECLINED,
                ):
                    raise StateConflictError(
                        f"旧指派 {old.id} 状态 {old.status.value}，不可被接管"
                    )

            new_official = self._require_official(new_official_id)
            if new_official.id == old.official_id:
                raise AssignmentError("替补裁判不能与原裁判为同一人")
            if self._occupying_for_match_official(match_id, new_official_id):
                raise StateConflictError("替补裁判已在该场次上有占用中的指派")

            verdict = evaluate_official(
                self.store, new_official, match, self.policy, ignore_match_id=match.id
            )
            if verdict.disqualified:
                raise EligibilityError("替补不具备硬性资格", reasons=verdict.disqualified)
            if verdict.exclusions:
                raise RuleExclusionError("替补被规则排除", reasons=verdict.exclusions)

            # 旧指派：REPLACED，保留完整时间线
            old.status = AssignmentStatus.REPLACED
            old.lock_version += 1
            old.add_event(
                actor=actor,
                action="replaced",
                reason=reason,
                successor_official_id=new_official_id,
            )

            takeover = Assignment(
                id=self.store.next_id("asn"),
                match_id=match_id,
                official_id=new_official_id,
                status=AssignmentStatus.CONFIRMED,
                created_at=self._clock(),
                replaces_assignment_id=old.id,
            )
            takeover.add_event(
                actor=actor,
                action="confirmed_takeover",
                reason=reason,
                predecessor_assignment_id=old.id,
            )
            self.store.assignments[takeover.id] = takeover
            return {
                "match_id": match_id,
                "old_assignment_id": old.id,
                "old_official_id": old.official_id,
                "new_assignment_id": takeover.id,
                "new_official_id": new_official_id,
                "status": takeover.status.value,
                "reason": reason,
            }

    # ---------- 查询 / 导出 ----------

    def assignment_timeline(self, assignment_id: str) -> dict:
        with self.store.lock():
            return self._require_assignment(assignment_id).to_dict()

    def export_by_date(self, day: date, tz_name: str = "Asia/Shanghai") -> dict:
        """按“比赛举办地时区的当地日期”导出口径，天然处理跨天与时区。"""
        with self.store.lock():
            rows = []
            for match in sorted(self.store.matches.values(), key=lambda m: m.start):
                if local_date_of(match.start, tz_name) != day:
                    continue
                assignments = [
                    a.to_dict()
                    for a in sorted(
                        self.store.assignments.values(),
                        key=lambda a: a.created_at,
                    )
                    if a.match_id == match.id
                ]
                rows.append(
                    {
                        "match": {
                            "id": match.id,
                            "level": match.level,
                            "sport": match.sport,
                            "city": match.city,
                            "venue": match.venue,
                            "home_team_id": match.home_team_id,
                            "away_team_id": match.away_team_id,
                            "start_local": match.start.astimezone(
                                ZoneInfo(tz_name)
                            ).isoformat(),
                            "end_local": match.end.astimezone(
                                ZoneInfo(tz_name)
                            ).isoformat(),
                            "started": match.is_started,
                            "finished": match.is_finished,
                        },
                        "assignments": assignments,
                    }
                )
            return {
                "date": day.isoformat(),
                "timezone": tz_name,
                "generated_at": self._clock().isoformat(),
                "match_count": len(rows),
                "matches": rows,
            }

    # ---------- 内部工具 ----------

    def _ensure_open(self, match: Match) -> None:
        if match.is_started:
            state = "已结束" if match.is_finished else "已开赛"
            raise HistoricalAssignmentProtected(
                f"比赛 {match.id} {state}，历史指派不可覆盖"
            )

    def _require_official(self, official_id: str) -> Official:
        if official_id not in self.store.officials:
            raise NotFoundError(f"裁判 {official_id} 不存在")
        return self.store.officials[official_id]

    def _require_match(self, match_id: str) -> Match:
        if match_id not in self.store.matches:
            raise NotFoundError(f"比赛 {match_id} 不存在")
        return self.store.matches[match_id]

    def _require_assignment(self, assignment_id: str) -> Assignment:
        if assignment_id not in self.store.assignments:
            raise NotFoundError(f"指派 {assignment_id} 不存在")
        return self.store.assignments[assignment_id]

    def health(self) -> dict[str, str]:
        return {"service": "assignment", "status": "ok"}
