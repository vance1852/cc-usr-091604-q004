"""利益冲突：申报后未开赛场次重新评估，已结束比赛历史保持原样。"""

import unittest
from datetime import datetime, timezone

from app.errors import NotEligibleError
from app.models import AssignmentStatus, MatchStatus
from app.service import AssignmentService
from app.testing import FakeClock, book_and_confirm

CST = "Asia/Shanghai"
DIRECTOR = "竞赛主任"


class ConflictTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 9, 18, 8, 0, tzinfo=timezone.utc))
        self.service = AssignmentService(clock=self.clock)
        for rid in ("R1", "R2"):
            self.service.add_referee(
                referee_id=rid,
                name=f"裁判{rid}",
                level="NATIONAL",
                events={"5v5"},
                regions={"华东"},
                home_city="上海",
            )

    def add_match(self, match_id, day=1, **overrides):
        params = {
            "name": f"比赛{match_id}",
            "level": "SEMIFINAL",
            "event": "5v5",
            "region": "华东",
            "city": "上海",
            "tz": CST,
            "start": datetime(2026, 10, day, 19, 0),
            "end": datetime(2026, 10, day, 21, 0),
            "teams": ("蓝鲸队", "猛虎队"),
        }
        params.update(overrides)
        return self.service.add_match(match_id=match_id, **params)

    def book(self, match_id, referee_id):
        return book_and_confirm(self.service, match_id, referee_id, director=DIRECTOR)[0]

    def test_new_conflict_flags_unstarted_assignments(self):
        match = self.add_match("C1")
        assignment = self.book(match.id, "R1")

        report = self.service.declare_conflict(
            "R1", "蓝鲸队", "TRAINING", "休赛期担任该队训练营讲师", actor="R1"
        )

        self.assertEqual(len(report["reevaluated"]), 1)
        entry = report["reevaluated"][0]
        self.assertEqual(entry["assignment_id"], assignment.id)
        self.assertEqual(entry["action"], "FLAGGED_CONFLICT")
        self.assertIn("培训关系", entry["explanation"])
        self.assertIn("蓝鲸队", entry["explanation"])
        self.assertEqual(assignment.status, AssignmentStatus.FLAGGED_CONFLICT)
        timeline = self.service.timeline(assignment.id)
        self.assertEqual(timeline[-1].action, "CONFLICT_FLAGGED")
        self.assertEqual(timeline[-1].actor, "R1")
        self.assertIn("训练营讲师", timeline[-1].reason)

    def test_finished_match_history_is_never_rewritten(self):
        played = self.add_match("C2", day=1)
        done = self.book(played.id, "R1")
        self.service.respond(
            done.id, referee_id="R1", accept=True, reason="确认执法"
        )
        self.service.set_match_status(played.id, MatchStatus.FINISHED, actor=DIRECTOR)
        upcoming = self.add_match("C3", day=2)
        pending = self.book(upcoming.id, "R1")
        before = [e.action for e in self.service.timeline(done.id)]

        report = self.service.declare_conflict("R1", "蓝鲸队", "RELATIVE", "亲属在该队任职")

        # 已结束比赛：指派状态与时间线完全不变，仅列入 skipped_finished 供查阅。
        self.assertEqual([e.action for e in self.service.timeline(done.id)], before)
        self.assertEqual(done.status, AssignmentStatus.ACCEPTED)
        self.assertEqual(
            [s["assignment_id"] for s in report["skipped_finished"]], [done.id]
        )
        # 未开赛场次：被重新评估并标记。
        self.assertEqual(pending.status, AssignmentStatus.FLAGGED_CONFLICT)
        self.assertEqual(
            [r["assignment_id"] for r in report["reevaluated"]], [pending.id]
        )

    def test_started_match_is_not_touched(self):
        match = self.add_match("C4", day=1)
        assignment = self.book(match.id, "R1")
        # 时钟推进到开赛之后、结束之前。
        self.clock.set(datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc))  # 上海 20:00

        report = self.service.declare_conflict("R1", "蓝鲸队", "OTHER", "临时申报")

        self.assertEqual(
            [s["assignment_id"] for s in report["skipped_started"]], [assignment.id]
        )
        self.assertEqual(assignment.status, AssignmentStatus.CONFIRMED)

    def test_conflict_excludes_from_candidates_and_explains(self):
        match = self.add_match("C5")
        self.service.declare_conflict("R1", "猛虎队", "FINANCIAL", "持有该队赞助商股份")

        report = self.service.generate_candidates(match.id)

        self.assertEqual([c.referee_id for c in report.candidates], ["R2"])
        exclusion = next(e for e in report.excluded if e.referee_id == "R1")
        self.assertEqual(exclusion.code, "CONFLICT_OF_INTEREST")
        self.assertEqual(exclusion.category, "conflict")

        explanation = self.service.explain(match.id, "R1")
        self.assertFalse(explanation["eligible"])
        reason = explanation["reasons"][0]
        self.assertEqual(reason["code"], "CONFLICT_OF_INTEREST")
        self.assertEqual(reason["conflict"]["kind"], "经济利益")
        self.assertEqual(reason["conflict"]["team_id"], "猛虎队")
        self.assertEqual(reason["conflict"]["note"], "持有该队赞助商股份")
        self.assertIn("declared_at", reason["conflict"])
        self.assertIn("经济利益", explanation["summary"])

    def test_all_qualified_excluded_by_conflict_reason(self):
        match = self.add_match("C6")
        self.service.declare_conflict("R1", "蓝鲸队", "TRAINING")
        self.service.declare_conflict("R2", "猛虎队", "RELATIVE")

        report = self.service.generate_candidates(match.id)

        self.assertEqual(report.candidates, [])
        self.assertEqual(report.empty_reason, "ALL_EXCLUDED_BY_CONFLICT")
        self.assertEqual(
            {e.category for e in report.excluded}, {"conflict"}
        )

    def test_substitute_resolves_flagged_assignment(self):
        match = self.add_match("C7")
        assignment = self.book(match.id, "R1")
        self.service.declare_conflict("R1", "蓝鲸队", "TRAINING", "训练营讲师")
        self.assertEqual(assignment.status, AssignmentStatus.FLAGGED_CONFLICT)

        result = self.service.substitute(
            assignment.id, director=DIRECTOR, reason="冲突待复核，启用替补"
        )

        self.assertTrue(result["substituted"])
        self.assertEqual(result["assignment"].referee_id, "R2")
        self.assertEqual(assignment.status, AssignmentStatus.SUPERSEDED)
        # 被换下的 R1 仍被排除在该场候选之外。
        report = self.service.generate_candidates(match.id)
        self.assertNotIn("R1", [c.referee_id for c in report.candidates])

    def test_deactivate_conflict_and_reinstate(self):
        match = self.add_match("C8")
        assignment = self.book(match.id, "R1")
        report = self.service.declare_conflict("R1", "蓝鲸队", "OTHER", "误报")
        conflict = report["conflict"]

        # 冲突仍生效时不允许直接恢复。
        with self.assertRaises(NotEligibleError):
            self.service.reinstate(assignment.id, director=DIRECTOR, reason="试图恢复")

        self.service.deactivate_conflict(conflict.id, actor=DIRECTOR, reason="核实为误报")
        self.service.reinstate(assignment.id, director=DIRECTOR, reason="误报已撤销，恢复指派")

        self.assertEqual(assignment.status, AssignmentStatus.CONFIRMED)
        timeline = self.service.timeline(assignment.id)
        self.assertEqual(timeline[-1].action, "REINSTATED")
        self.assertEqual(timeline[-1].reason, "误报已撤销，恢复指派")
        candidates = self.service.generate_candidates(match.id)
        # R1 已被确认占用该场，候选中只剩 R2；关键是 R1 不再因冲突被排除。
        self.assertNotIn(
            "CONFLICT_OF_INTEREST", {e.code for e in candidates.excluded}
        )


if __name__ == "__main__":
    unittest.main()
