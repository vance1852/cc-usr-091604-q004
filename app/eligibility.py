"""资格与候选评估引擎（纯读逻辑，调用方需持锁或传入快照）。

输出刻意分成三类，便于接口区分“未找到合格人选”和“被冲突/排班规则排除”：

- candidates：通过全部硬性资格与软性规则的裁判；
- excluded：硬性资格合格、但被利益冲突/不可用/占用/排班规则排除；
- disqualified：硬性资格（项目、等级、执法区域、在职状态）就不满足。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from .models import (
    MATCH_MIN_GRADE,
    Assignment,
    Match,
    Official,
)
from .policy import Policy

# 排除原因代码（冲突/排班规则）
REASON_CONFLICT = "conflict"
REASON_UNAVAILABLE = "unavailable"
REASON_OCCUPIED = "double_booked"
REASON_ALREADY_ON_MATCH = "already_on_match"
REASON_TRAVEL = "travel_gap"
REASON_CONSECUTIVE = "consecutive_hours"
REASON_RECENT_LOAD = "recent_load"

# 硬性不合格原因代码
DQ_INACTIVE = "inactive"
DQ_SPORT = "sport_not_qualified"
DQ_GRADE = "grade_too_low"
DQ_REGION = "region_not_covered"


@dataclass
class Reason:
    code: str
    message: str
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


@dataclass
class OfficialVerdict:
    official_id: str
    name: str
    eligible: bool
    disqualified: list[Reason] = field(default_factory=list)
    exclusions: list[Reason] = field(default_factory=list)


def _hard_qualifications(
    official: Official, match: Match
) -> list[Reason]:
    reasons: list[Reason] = []
    if not official.active:
        reasons.append(Reason(DQ_INACTIVE, f"{official.name} 已停用，不可被指派"))
    if not official.can_ref_sport(match.sport):
        reasons.append(
            Reason(
                DQ_SPORT,
                f"{official.name} 不具备 {match.sport} 执法项目资格",
                {"required": match.sport, "official_sports": sorted(official.sports)},
            )
        )
    required = MATCH_MIN_GRADE[match.level]
    if official.grade.rank > required.rank:
        reasons.append(
            Reason(
                DQ_GRADE,
                f"{official.name} 等级 {official.grade.value} 低于 {match.level} 比赛要求的 {required.value}",
                {"required_grade": required.value, "official_grade": official.grade.value},
            )
        )
    if not official.covers_city(match.city):
        reasons.append(
            Reason(
                DQ_REGION,
                f"{official.name} 的执法区域不包含 {match.city}",
                {"home_city": official.home_city, "region": sorted(official.region_cities)},
            )
        )
    return reasons


def _active_conflicts(store, official: Official, match: Match) -> list[Reason]:
    reasons: list[Reason] = []
    team_ids = {match.home_team_id, match.away_team_id}
    for c in store.conflicts:
        if not c.active or c.official_id != official.id or c.team_id not in team_ids:
            continue
        team = store.teams.get(c.team_id)
        team_name = team.name if team else c.team_id
        reasons.append(
            Reason(
                REASON_CONFLICT,
                f"{official.name} 主动申报与 {team_name} 存在{c.conflict_type.value}利益冲突"
                + (f"（{c.detail}）" if c.detail else ""),
                {
                    "declaration_id": c.id,
                    "team_id": c.team_id,
                    "team_name": team_name,
                    "conflict_type": c.conflict_type.value,
                },
            )
        )
    return reasons


def _unavailability_reasons(store, official: Official, match: Match) -> list[Reason]:
    reasons: list[Reason] = []
    for u in store.unavailabilities:
        if u.official_id != official.id:
            continue
        if u.overlaps(match.start, match.end):
            reasons.append(
                Reason(
                    REASON_UNAVAILABLE,
                    f"{official.name} 在比赛时段已登记不可用：{u.reason or '未填写原因'}",
                    {
                        "unavailability_id": u.id,
                        "blocked_from": u.start.isoformat(),
                        "blocked_to": u.end.isoformat(),
                    },
                )
            )
    return reasons


def _occupying_assignments(store, official_id: str) -> list[Assignment]:
    out = []
    for a in store.assignments.values():
        if a.official_id == official_id and a.status.occupies:
            out.append(a)
    return out


def _schedule_reasons(
    store,
    official: Official,
    match: Match,
    others: list[tuple[Assignment, Match]],
    policy: Policy,
) -> list[Reason]:
    """基于其他已占用场次检查转场距离、连续工作时长与近期场次。"""
    reasons: list[Reason] = []
    # 按开始时间排序的工作项（不含目标比赛）
    ordered = sorted(others, key=lambda pair: pair[1].start)

    # 1) 时间直接重叠 / 转场不足
    for assignment, other in ordered:
        if other.start < match.end and match.start < other.end:
            reasons.append(
                Reason(
                    REASON_OCCUPIED,
                    f"{official.name} 该时段已被比赛 {other.id}（指派 {assignment.id}，"
                    f"状态 {assignment.status.value}）占用",
                    {"other_match_id": other.id, "assignment_id": assignment.id},
                )
            )
            continue
        if other.end <= match.start:
            gap_min = (match.start - other.end).total_seconds() / 60.0
            from_city, to_city = other.city, match.city
        else:
            gap_min = (other.start - match.end).total_seconds() / 60.0
            from_city, to_city = match.city, other.city
        distance = store.cities.distance_km(from_city, to_city)
        required_gap = policy.required_gap_minutes(distance)
        if gap_min < required_gap:
            reasons.append(
                Reason(
                    REASON_TRAVEL,
                    f"{official.name} 从 {from_city} 跨城赶往 {to_city}（约 {distance} 公里）转场时间不足："
                    f"实际间隔 {gap_min:.0f} 分钟，需要 {required_gap:.0f} 分钟",
                    {
                        "other_match_id": other.id,
                        "from_city": from_city,
                        "to_city": to_city,
                        "distance_km": None if distance == float("inf") else distance,
                        "gap_minutes": round(gap_min, 1),
                        "required_minutes": None
                        if required_gap == float("inf")
                        else round(required_gap, 1),
                    },
                )
            )

    # 2) 连续工作时长：把目标比赛插入时间线后按 block_join_minutes 归组
    items = [(m.start, m.end, m.id) for _, m in ordered]
    items.append((match.start, match.end, match.id))
    items.sort(key=lambda x: x[0])
    blocks: list[tuple] = []  # (起, 止, 场次列表)
    block_start = items[0][0]
    block_prev_end = items[0][1]
    block_members = [items[0][2]]
    for start, end, mid in items[1:]:
        gap = (start - block_prev_end).total_seconds() / 60.0
        if gap <= policy.block_join_minutes:
            block_prev_end = max(block_prev_end, end)
            block_members.append(mid)
        else:
            blocks.append((block_start, block_prev_end, block_members))
            block_start, block_prev_end, block_members = start, end, [mid]
    blocks.append((block_start, block_prev_end, block_members))

    for b_start, b_end, members in blocks:
        span = (b_end - b_start).total_seconds() / 3600.0
        if span > policy.max_consecutive_hours:
            reasons.append(
                Reason(
                    REASON_CONSECUTIVE,
                    f"{official.name} 连续工作 {span:.1f} 小时，超过上限 {policy.max_consecutive_hours} 小时",
                    {
                        "consecutive_hours": round(span, 2),
                        "limit_hours": policy.max_consecutive_hours,
                        "matches": list(members),
                    },
                )
            )

    # 3) 近期场次（窗口内已占用场次 + 本场）
    window = timedelta(hours=policy.recent_window_hours)
    prior = [
        (a, m)
        for a, m in ordered
        if match.start - window <= m.start < match.start
    ]
    if len(prior) + 1 > policy.max_recent_games:
        reasons.append(
            Reason(
                REASON_RECENT_LOAD,
                f"{official.name} 近 {policy.recent_window_hours} 小时内已有 {len(prior)} 场，"
                f"加上本场将超过 {policy.max_recent_games} 场上限",
                {
                    "window_hours": policy.recent_window_hours,
                    "prior_games": len(prior),
                    "limit": policy.max_recent_games,
                    "prior_match_ids": [m.id for _, m in prior],
                },
            )
        )
    return reasons


def evaluate_official(
    store,
    official: Official,
    match: Match,
    policy: Policy,
    *,
    ignore_match_id: Optional[str] = None,
    ignore_assignment_id: Optional[str] = None,
) -> OfficialVerdict:
    """评估单个裁判对一场比赛的可指派性。

    ignore_match_id：排班检查时跳过该场次的既有指派（候选生成场景）。
    ignore_assignment_id：复核在任指派（锁定/确认/重评估）时跳过指派自身。
    """
    verdict = OfficialVerdict(official_id=official.id, name=official.name, eligible=False)

    hard = _hard_qualifications(official, match)
    if hard:
        verdict.disqualified.extend(hard)
        return verdict

    def _kept(a: Assignment) -> bool:
        return a.id != ignore_assignment_id

    # 已在该场比赛上有占用中的指派（候选重算时避免重复出现）
    same_match = [
        a
        for a in _occupying_assignments(store, official.id)
        if _kept(a) and a.match_id == match.id
    ]
    if same_match:
        verdict.exclusions.append(
            Reason(
                REASON_ALREADY_ON_MATCH,
                f"{official.name} 已是比赛 {match.id} 的在队指派裁判",
                {"assignment_ids": [a.id for a in same_match]},
            )
        )

    verdict.exclusions.extend(_active_conflicts(store, official, match))
    verdict.exclusions.extend(_unavailability_reasons(store, official, match))

    others: list[tuple[Assignment, Match]] = []
    for a in _occupying_assignments(store, official.id):
        if not _kept(a):
            continue
        if ignore_match_id and a.match_id == ignore_match_id:
            continue
        other_match = store.matches.get(a.match_id)
        if other_match is not None:
            others.append((a, other_match))
    verdict.exclusions.extend(_schedule_reasons(store, official, match, others, policy))

    verdict.eligible = not verdict.exclusions
    return verdict


def _candidate_score(
    store, official: Official, match: Match, policy: Policy
) -> tuple:
    """确定性排序：距离近 → 近期场次少 → 等级高 → id。"""
    distance = store.cities.distance_km(official.home_city, match.city)
    window = timedelta(hours=policy.recent_window_hours)
    recent = 0
    for a in store.assignments.values():
        if a.official_id != official.id or not a.status.occupies:
            continue
        m = store.matches.get(a.match_id)
        if m is not None and match.start - window <= m.start < match.start:
            recent += 1
    distance_key = float("inf") if distance == float("inf") else distance
    return (distance_key, recent, official.grade.rank, official.id)


def generate_candidates(
    store, match: Match, policy: Policy, *, limit: Optional[int] = None
) -> dict:
    """生成候选名单及完整的排除/不合格解释。"""
    from .timeutils import now_utc

    candidates: list[dict] = []
    excluded: list[dict] = []
    disqualified: list[dict] = []

    for official in store.officials.values():
        verdict = evaluate_official(
            store, official, match, policy, ignore_match_id=match.id
        )
        if verdict.disqualified:
            disqualified.append(_verdict_dict(verdict))
        elif verdict.exclusions:
            excluded.append(_verdict_dict(verdict))
        else:
            distance = store.cities.distance_km(official.home_city, match.city)
            score = _candidate_score(store, official, match, policy)
            candidates.append(
                {
                    "official_id": official.id,
                    "name": official.name,
                    "grade": official.grade.value,
                    "home_city": official.home_city,
                    "distance_km": None if distance == float("inf") else distance,
                    "score": {
                        "distance_km": score[0] if score[0] != float("inf") else None,
                        "recent_games": score[1],
                        "grade_rank": score[2],
                    },
                }
            )

    candidates.sort(
        key=lambda c: (
            c["score"]["distance_km"] is None,
            c["score"]["distance_km"] if c["score"]["distance_km"] is not None else 0,
            c["score"]["recent_games"],
            c["score"]["grade_rank"],
            c["official_id"],
        )
    )
    for rank, c in enumerate(candidates, start=1):
        c["rank"] = rank
    if limit is not None:
        candidates = candidates[:limit]

    if candidates:
        outcome = "candidates_available"
    elif excluded:
        outcome = "all_excluded_by_rules"
    else:
        outcome = "no_qualified_pool"

    return {
        "match_id": match.id,
        "generated_at": now_utc().isoformat(),
        "policy": {
            "max_consecutive_hours": policy.max_consecutive_hours,
            "max_recent_games": policy.max_recent_games,
            "recent_window_hours": policy.recent_window_hours,
            "travel_kmh": policy.travel_kmh,
        },
        "candidates": candidates,
        "excluded": excluded,
        "disqualified": disqualified,
        "summary": {
            "total_officials": len(store.officials),
            "eligible": len(candidates),
            "excluded": len(excluded),
            "disqualified": len(disqualified),
            "outcome": outcome,
        },
    }


def _verdict_dict(verdict: OfficialVerdict) -> dict:
    return {
        "official_id": verdict.official_id,
        "name": verdict.name,
        "reasons": [r.to_dict() for r in (verdict.disqualified or verdict.exclusions)],
    }


def explain_conflicts(store, match: Match, official: Official) -> dict:
    """冲突解释接口：返回利益冲突申报与排班层面的全部冲突。"""
    verdict = evaluate_official(store, official, match, Policy(), ignore_match_id=match.id)
    conflict_reasons = [r for r in verdict.exclusions if r.code == REASON_CONFLICT]
    schedule_reasons = [
        r
        for r in verdict.exclusions
        if r.code
        in (
            REASON_UNAVAILABLE,
            REASON_OCCUPIED,
            REASON_TRAVEL,
            REASON_CONSECUTIVE,
            REASON_RECENT_LOAD,
            REASON_ALREADY_ON_MATCH,
        )
    ]
    return {
        "match_id": match.id,
        "official_id": official.id,
        "official_name": official.name,
        "interest_conflicts": [r.to_dict() for r in conflict_reasons],
        "schedule_conflicts": [r.to_dict() for r in schedule_reasons],
        "disqualified": [r.to_dict() for r in verdict.disqualified],
        "assignable": verdict.eligible,
    }
