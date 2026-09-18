"""指派流程：锁定 → 确认 → 接受/拒绝 → 撤销/替补/批量，全程留痕。"""

import unittest
from datetime import datetime, timezone

from app.errors import (
    InvalidStateError,
    MatchFinishedError,
    NotEligibleError,
    ReasonRequiredError,
    VersionConflictError,
)
from app.models import AssignmentStatus, MatchStatus
from app.service import AssignmentService
from app.testing import FakeClock, book_and_confirm

CST = "Asia/Shanghai"
DIRECTOR = "竞赛主任"


class AssignmentFlowTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 9, 18, 8, 0, tzinfo=timezone.utc))
        self.service = AssignmentService(clock=self.clock)
        for rid, city in [("R1", "上海"), ("R2", "上海"), ("R3", "杭州")]:
            self.service.add_referee(
                referee_id=rid,
                name=f"裁判{rid}",
                level="NATIONAL",
                events={"5v5"},
                regions={"华东"},
                home_city=city,
            )

    def add_match(self, match_id="M1", **overrides):
        params = {
            "name": f"比赛{match_id}",
            "level": "SEMIFINAL",
            "event": "5v5",
            "region": "华东",
            "city": "上海",
            "tz": CST,
            "start": datetime(2026, 10, 1, 19, 0),
            "end": datetime(2026, 10, 1, 21, 0),
            "teams": ("蓝鲸队", "猛虎队"),
        }
        params.update(overrides)
        return self.service.add_match(match_id=match_id, **params)

    def book(self, match_id, referee_id):
        return book_and_confirm(self.service, match_id, referee_id, director=DIRECTOR)[0]

    # ---- 锁定 / 确认 / 响应 ----

    def test_lock_confirm_accept_keeps_full_timeline(self):
        match = self.add_match()
        lock = self.service.lock_candidates(match.id, ["R1"], director=DIRECTOR)
        self.assertEqual(lock["lock_token"], 1)

        confirmed = self.service.confirm(
            match.id, expected_version=lock["lock_token"], director=DIRECTOR
        )
        assignment = confirmed[0]
        self.assertEqual(assignment.status, AssignmentStatus.CONFIRMED)

        self.clock.advance(minutes=5)
        self.service.respond(
            assignment.id, referee_id="R1", accept=True, reason="档期合适，可以执法"
        )

        timeline = self.service.timeline(assignment.id)
        self.assertEqual(
            [e.action for e in timeline], ["LOCKED", "CONFIRMED", "ACCEPTED"]
        )
        self.assertEqual([e.actor for e in timeline], [DIRECTOR, DIRECTOR, "R1"])
        self.assertEqual(timeline[-1].reason, "档期合适，可以执法")
        seqs = [e.seq for e in timeline]
        self.assertEqual(seqs, sorted(seqs))
        # 时间线时间戳来自注入时钟，可复现、可追溯。
        self.assertEqual(
            timeline[0].at, datetime(2026, 9, 18, 8, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(
            timeline[-1].at, datetime(2026, 9, 18, 8, 5, tzinfo=timezone.utc)
        )

    def test_respond_requires_reason_for_accept_and_decline(self):
        match = self.add_match()
        assignment = self.book(match.id, "R1")
        with self.assertRaises(ReasonRequiredError):
            self.service.respond(assignment.id, referee_id="R1", accept=True, reason="")
        with self.assertRaises(ReasonRequiredError):
            self.service.respond(
                assignment.id, referee_id="R1", accept=False, reason="   "
            )

    def test_respond_only_by_assigned_referee_and_once(self):
        match = self.add_match()
        assignment = self.book(match.id, "R1")
        with self.assertRaises(InvalidStateError):
            self.service.respond(
                assignment.id, referee_id="R2", accept=True, reason="冒名响应"
            )
        self.service.respond(
            assignment.id, referee_id="R1", accept=False, reason="家中有事"
        )
        with self.assertRaises(InvalidStateError):
            self.service.respond(
                assignment.id, referee_id="R1", accept=True, reason="改主意了"
            )

    def test_decline_then_substitute_takeover(self):
        match = self.add_match()
        assignment = self.book(match.id, "R1")
        self.service.respond(
            assignment.id, referee_id="R1", accept=False, reason="临时出差"
        )

        result = self.service.substitute(
            assignment.id, director=DIRECTOR, reason="原裁判拒绝，安排替补"
        )

        self.assertTrue(result["substituted"])
        new = result["assignment"]
        self.assertEqual(new.referee_id, "R2")  # 同城候选优先
        self.assertEqual(new.status, AssignmentStatus.CONFIRMED)
        self.assertEqual(new.slot, assignment.slot)
        old_timeline = [e.action for e in self.service.timeline(assignment.id)]
        self.assertEqual(
            old_timeline, ["LOCKED", "CONFIRMED", "DECLINED", "SUPERSEDED"]
        )
        new_timeline = self.service.timeline(new.id)
        self.assertEqual(new_timeline[0].action, "TAKEOVER")
        self.assertIn("R1", new_timeline[0].detail)
        self.assertEqual(new_timeline[0].reason, "原裁判拒绝，安排替补")

    def test_substitute_rejects_active_assignment(self):
        match = self.add_match()
        assignment = self.book(match.id, "R1")
        with self.assertRaises(InvalidStateError):
            self.service.substitute(
                assignment.id, director=DIRECTOR, reason="不能替换生效中的指派"
            )

    # ---- 撤销 ----

    def test_revoke_frees_referee_and_keeps_reason(self):
        match = self.add_match()
        assignment = self.book(match.id, "R1")
        self.clock.advance(minutes=10)
        self.service.revoke(assignment.id, director=DIRECTOR, reason="赛程调整")

        self.assertEqual(assignment.status, AssignmentStatus.REVOKED)
        timeline = self.service.timeline(assignment.id)
        self.assertEqual(timeline[-1].action, "REVOKED")
        self.assertEqual(timeline[-1].reason, "赛程调整")
        self.assertEqual(timeline[-1].actor, DIRECTOR)
        # 撤销后同一裁判可以承接同一时间段的另一场比赛。
        other = self.add_match(match_id="M2", name="同时段另一场")
        report = self.service.generate_candidates(other.id)
        self.assertIn("R1", [c.referee_id for c in report.candidates])

    def test_revoke_requires_reason(self):
        match = self.add_match()
        assignment = self.book(match.id, "R1")
        with self.assertRaises(ReasonRequiredError):
            self.service.revoke(assignment.id, director=DIRECTOR, reason="")

    # ---- 已结束比赛的历史指派不可覆盖 ----

    def test_finished_match_assignment_is_immutable(self):
        match = self.add_match()
        assignment = self.book(match.id, "R1")
        self.service.respond(
            assignment.id, referee_id="R1", accept=True, reason="确认执法"
        )
        self.service.set_match_status(match.id, MatchStatus.FINISHED, actor=DIRECTOR)
        before = [e.action for e in self.service.timeline(assignment.id)]

        with self.assertRaises(MatchFinishedError):
            self.service.revoke(assignment.id, director=DIRECTOR, reason="试图改历史")
        with self.assertRaises(MatchFinishedError):
            self.service.respond(
                assignment.id, referee_id="R1", accept=False, reason="试图改历史"
            )
        with self.assertRaises(MatchFinishedError):
            self.service.lock_candidates(match.id, ["R2"], director=DIRECTOR)
        with self.assertRaises(MatchFinishedError):
            self.service.substitute(assignment.id, director=DIRECTOR, reason="试图改历史")

        after = [e.action for e in self.service.timeline(assignment.id)]
        self.assertEqual(before, after)
        self.assertEqual(assignment.status, AssignmentStatus.ACCEPTED)

    # ---- 乐观锁 ----

    def test_confirm_with_stale_version_fails(self):
        match = self.add_match()
        first = self.service.lock_candidates(match.id, ["R1"], director=DIRECTOR)
        second = self.service.lock_candidates(match.id, ["R2"], director=DIRECTOR)
        self.assertNotEqual(first["lock_token"], second["lock_token"])
        with self.assertRaises(VersionConflictError) as ctx:
            self.service.confirm(
                match.id, expected_version=first["lock_token"], director=DIRECTOR
            )
        self.assertEqual(ctx.exception.details["actual"], second["lock_token"])

    def test_lock_rejects_ineligible_candidate_with_details(self):
        self.service.add_referee(
            referee_id="R9",
            name="低等级裁判",
            level="LEVEL_2",
            events={"5v5"},
            regions={"华东"},
            home_city="上海",
        )
        match = self.add_match()
        with self.assertRaises(NotEligibleError) as ctx:
            self.service.lock_candidates(match.id, ["R9"], director=DIRECTOR)
        codes = {e["code"] for e in ctx.exception.details["R9"]}
        self.assertEqual(codes, {"LEVEL_TOO_LOW"})

    # ---- 批量指派 ----

    def test_batch_assign_never_double_books(self):
        m1 = self.add_match("B1", start=datetime(2026, 10, 1, 19, 0),
                            end=datetime(2026, 10, 1, 21, 0))
        m2 = self.add_match("B2", start=datetime(2026, 10, 1, 19, 30),
                            end=datetime(2026, 10, 1, 21, 30))
        m3 = self.add_match("B3", start=datetime(2026, 10, 2, 19, 0),
                            end=datetime(2026, 10, 2, 21, 0))

        result = self.service.batch_assign([m3.id, m1.id, m2.id], director=DIRECTOR)
        by_match = {r["match_id"]: r for r in result["results"]}

        # 同一时段的两场必须分给不同裁判，避免重复占用。
        self.assertEqual(by_match[m1.id]["status"], "ASSIGNED")
        self.assertEqual(by_match[m2.id]["status"], "ASSIGNED")
        self.assertNotEqual(
            by_match[m1.id]["referee_ids"], by_match[m2.id]["referee_ids"]
        )
        self.assertEqual(by_match[m3.id]["status"], "ASSIGNED")
        for row in result["results"]:
            if row["status"] == "ASSIGNED":
                for assignment_id in row["assigned"]:
                    timeline = self.service.timeline(assignment_id)
                    self.assertEqual(timeline[0].action, "CONFIRMED")
                    self.assertEqual(timeline[0].reason, "批量指派")

    def test_batch_assign_reports_unstaffed_with_reason(self):
        # 只有一名合格裁判，两场同时段比赛 → 第二场无人可派。
        m1 = self.add_match("U1", start=datetime(2026, 10, 1, 19, 0),
                            end=datetime(2026, 10, 1, 21, 0))
        m2 = self.add_match("U2", start=datetime(2026, 10, 1, 19, 30),
                            end=datetime(2026, 10, 1, 21, 30))
        for rid in ("R2", "R3"):
            self.service.declare_conflict(rid, "蓝鲸队", "TRAINING", "暑期训练营")

        result = self.service.batch_assign([m1.id, m2.id], director=DIRECTOR)
        by_match = {r["match_id"]: r for r in result["results"]}

        staffed, unstaffed = (
            (by_match[m1.id], by_match[m2.id])
            if by_match[m1.id]["status"] == "ASSIGNED"
            else (by_match[m2.id], by_match[m1.id])
        )
        self.assertEqual(staffed["referee_ids"], ["R1"])
        self.assertEqual(unstaffed["status"], "UNSTAFFED")
        # R2/R3 被冲突规则排除、R1 被重复占用排除 → 仍有合格人选被冲突排除。
        self.assertEqual(unstaffed["empty_reason"], "ALL_EXCLUDED_BY_CONFLICT")
        categories = {e["category"] for e in unstaffed["excluded"]}
        self.assertIn("conflict", categories)

    def test_batch_assign_skips_finished_matches(self):
        match = self.add_match()
        self.service.set_match_status(match.id, MatchStatus.FINISHED, actor=DIRECTOR)
        result = self.service.batch_assign([match.id], director=DIRECTOR)
        self.assertEqual(result["results"][0]["status"], "SKIPPED")


if __name__ == "__main__":
    unittest.main()
