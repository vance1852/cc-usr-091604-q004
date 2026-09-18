"""服务生命周期：锁定/确认/响应、冲突重评估、历史保护、替补、批量、导出。"""

import unittest
from datetime import date

from app.models import AssignmentStatus
from app.service import (
    AssignmentError,
    EligibilityError,
    HistoricalAssignmentProtected,
    RuleExclusionError,
    StateConflictError,
)

from tests.fixtures import build_league, make_match


class AssignmentLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_league()
        self.match = make_match(self.svc)

    def test_lock_confirm_accept_timeline(self):
        a = self.svc.lock_candidate("m1", "r_zhou", "主任李")
        self.assertEqual(a.status, AssignmentStatus.LOCKED)
        self.svc.confirm_assignment(a.id, "主任李", expected_version=1)
        accepted = self.svc.respond(a.id, True, "已核对日程，可以执法", actor="r_zhou")
        self.assertEqual(accepted.status, AssignmentStatus.ACCEPTED)
        tl = self.svc.assignment_timeline(a.id)["timeline"]
        actions = [e["action"] for e in tl]
        self.assertEqual(actions, ["locked", "confirmed", "accepted"])
        # 拒绝/接受理由都被保留
        self.assertEqual(tl[-1]["reason"], "已核对日程，可以执法")
        self.assertEqual(tl[-1]["actor"], "r_zhou")
        self.assertIsNotNone(tl[-1]["at"])

    def test_response_requires_reason(self):
        a = self.svc.lock_candidate("m1", "r_zhou", "主任李")
        self.svc.confirm_assignment(a.id, "主任李")
        with self.assertRaises(AssignmentError):
            self.svc.respond(a.id, False, "  ", actor="r_zhou")

    def test_decline_records_reason_and_frees_official(self):
        # 先安排一场冲突比赛，接受；第二场若也想派周宁会被占用
        other = make_match(
            self.svc, "m2", start="2026-09-20T19:00", end="2026-09-20T21:00",
            city="杭州",
        )
        # m1 上海 19:00 与 m2 杭州 19:00 时间重叠
        a = self.svc.lock_candidate("m1", "r_zhou", "主任李")
        self.svc.confirm_assignment(a.id, "主任李")
        declined = self.svc.respond(a.id, False, "家中突发急事", actor="r_zhou")
        self.assertEqual(declined.status, AssignmentStatus.DECLINED)
        # 拒绝后不再占用：该裁判重新出现在候选中
        res = self.svc.candidates("m1")
        self.assertIn("r_zhou", [c["official_id"] for c in res["candidates"]])

    def test_cannot_lock_same_official_twice(self):
        self.svc.lock_candidate("m1", "r_zhou", "主任李")
        with self.assertRaises(StateConflictError):
            self.svc.lock_candidate("m1", "r_zhou", "主任李")

    def test_lock_unqualified_raises_not_qualified(self):
        with self.assertRaises(EligibilityError) as ctx:
            self.svc.lock_candidate("m1", "r_low", "主任李")
        self.assertEqual(ctx.exception.code, "not_qualified")
        self.assertTrue(
            any(r.code == "grade_too_low" for r in ctx.exception.reasons)
        )

    def test_lock_conflicted_raises_excluded(self):
        self.svc.declare_conflict("r_zhou", "t_sh", "family", "配偶在队")
        with self.assertRaises(RuleExclusionError) as ctx:
            self.svc.lock_candidate("m1", "r_zhou", "主任李")
        self.assertEqual(ctx.exception.code, "excluded_by_rules")
        self.assertTrue(
            any(r.code == "conflict" for r in ctx.exception.reasons)
        )


class ConflictReevaluationTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_league()

    def test_new_conflict_supersedes_upcoming_assignment(self):
        make_match(self.svc, "m1")
        make_match(
            self.svc, "m2", start="2026-09-21T19:00", end="2026-09-21T21:00"
        )
        a1 = self.svc.lock_candidate("m1", "r_zhou", "主任李")
        self.svc.confirm_assignment(a1.id, "主任李")
        self.svc.respond(a1.id, True, "可执法")
        a2 = self.svc.lock_candidate("m2", "r_zhou", "主任李")
        self.svc.confirm_assignment(a2.id, "主任李")

        result = self.svc.declare_conflict("r_zhou", "t_sh", "training", "受聘培训")
        # 两场未开赛都应被重评估并作废
        self.assertEqual(
            set(result["reevaluated"]["superseded_assignment_ids"]), {a1.id, a2.id}
        )
        self.assertEqual(
            self.svc.assignment_timeline(a1.id)["status"],
            AssignmentStatus.SUPERSEDED.value,
        )
        # 时间线保留重评估原因与触发来源
        tl = self.svc.assignment_timeline(a1.id)["timeline"]
        reeval = tl[-1]
        self.assertTrue(reeval["action"].startswith("reevaluate:conflict_declared"))
        self.assertIn("training", reeval["reason"])
        self.assertEqual(reeval["detail"]["reason_codes"], ["conflict"])

    def test_finished_match_assignment_is_never_overwritten(self):
        make_match(self.svc, "m1")
        a1 = self.svc.lock_candidate("m1", "r_zhou", "主任李")
        self.svc.confirm_assignment(a1.id, "主任李")
        self.svc.respond(a1.id, True, "可执法")
        self.svc.mark_started("m1")
        self.svc.mark_finished("m1")

        # 新增冲突：历史指派保持 accepted 不变
        result = self.svc.declare_conflict("r_zhou", "t_sh", "family")
        self.assertEqual(result["reevaluated"]["superseded_assignment_ids"], [])
        self.assertIn("m1", result["reevaluated"]["finished_matches_protected"])
        self.assertEqual(
            self.svc.assignment_timeline(a1.id)["status"],
            AssignmentStatus.ACCEPTED.value,
        )
        # 也无法对已结束比赛做取消/接管/响应
        with self.assertRaises(HistoricalAssignmentProtected):
            self.svc.cancel_assignment(a1.id, "事后撤销", "主任李")
        with self.assertRaises(HistoricalAssignmentProtected):
            self.svc.substitute_takeover("m1", "r_wu", "想换人", "主任李")


class SubstitutionTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_league()
        make_match(self.svc, "m1")

    def test_takeover_after_decline_keeps_timeline(self):
        a = self.svc.lock_candidate("m1", "r_zhou", "主任李")
        self.svc.confirm_assignment(a.id, "主任李")
        self.svc.respond(a.id, False, "赛前受伤")

        result = self.svc.substitute_takeover("m1", "r_wu", "原裁判受伤需替补", "主任李")
        self.assertEqual(result["old_assignment_id"], a.id)
        self.assertEqual(result["new_official_id"], "r_wu")
        old = self.svc.assignment_timeline(a.id)
        self.assertEqual(old["status"], AssignmentStatus.REPLACED.value)
        actions = [e["action"] for e in old["timeline"]]
        self.assertEqual(actions, ["locked", "confirmed", "declined", "replaced"])
        new = self.svc.assignment_timeline(result["new_assignment_id"])
        self.assertEqual(new["status"], AssignmentStatus.CONFIRMED.value)
        self.assertEqual(new["replaces_assignment_id"], a.id)
        self.assertEqual(new["timeline"][0]["detail"]["predecessor_assignment_id"], a.id)

    def test_takeover_rejects_conflicted_substitute(self):
        a = self.svc.lock_candidate("m1", "r_zhou", "主任李")
        self.svc.confirm_assignment(a.id, "主任李")
        self.svc.declare_conflict("r_wu", "t_sh", "employment", "在赞助单位任职")
        with self.assertRaises(RuleExclusionError) as ctx:
            self.svc.substitute_takeover("m1", "r_wu", "替换", "主任李")
        self.assertTrue(any(r.code == "conflict" for r in ctx.exception.reasons))


class BatchAssignTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_league()

    def test_batch_commits_all_or_nothing(self):
        make_match(self.svc, "m1", city="上海",
                   start="2026-09-22T19:00", end="2026-09-22T21:00")
        make_match(self.svc, "m2", city="杭州",
                   start="2026-09-23T19:00", end="2026-09-23T21:00")
        result = self.svc.batch_assign(
            [
                {"match_id": "m1", "official_id": "r_zhou"},
                {"match_id": "m2", "official_id": "r_wu"},
            ],
            "主任李",
        )
        self.assertTrue(result["committed"])
        self.assertEqual(len(result["results"]), 2)

    def test_batch_rolls_back_on_conflict_and_avoids_double_booking(self):
        # 同一裁判被排到两场时间重叠的比赛
        make_match(self.svc, "m1", city="杭州",
                   start="2026-09-22T19:00", end="2026-09-22T21:00")
        make_match(self.svc, "m2", city="杭州",
                   start="2026-09-22T19:30", end="2026-09-22T21:30")
        result = self.svc.batch_assign(
            [
                {"match_id": "m1", "official_id": "r_wu"},
                {"match_id": "m2", "official_id": "r_wu"},
            ],
            "主任李",
        )
        self.assertFalse(result["committed"])
        self.assertEqual(len(result["failures"]), 1)
        failure = result["failures"][0]
        self.assertEqual(failure["index"], 1)
        self.assertEqual(failure["error_code"], "excluded_by_rules")
        codes = {r["code"] for r in failure["reasons"]}
        self.assertIn("double_booked", codes)
        # 回滚：没有任何指派落库
        self.assertEqual(list(self.svc.store.assignments.values()), [])

    def test_batch_reports_not_qualified_distinctly(self):
        make_match(self.svc, "m1", city="上海")
        result = self.svc.batch_assign(
            [{"match_id": "m1", "official_id": "r_low"}], "主任李"
        )
        self.assertFalse(result["committed"])
        self.assertEqual(result["failures"][0]["error_code"], "not_qualified")

    def test_batch_enforces_recent_load_across_requests(self):
        from app.policy import Policy
        svc = build_league(policy=Policy(max_recent_games=2, recent_window_hours=24))
        # 同一裁判、同一城市、24 小时内排 3 场 => 第 3 场触发 recent_load，整批回滚
        slots = [
            ("m1", "2026-09-22T09:00", "2026-09-22T11:00"),
            ("m2", "2026-09-22T15:00", "2026-09-22T17:00"),
            ("m3", "2026-09-22T20:00", "2026-09-22T22:00"),
        ]
        for mid, start, end in slots:
            make_match(svc, mid, city="上海", start=start, end=end)
        result = svc.batch_assign(
            [
                {"match_id": "m1", "official_id": "r_zhou"},
                {"match_id": "m2", "official_id": "r_zhou"},
                {"match_id": "m3", "official_id": "r_zhou"},
            ],
            "主任李",
        )
        self.assertFalse(result["committed"])
        failure = result["failures"][0]
        self.assertEqual(failure["index"], 2)
        codes = {r["code"] for r in failure["reasons"]}
        self.assertIn("recent_load", codes)
        self.assertEqual(list(svc.store.assignments.values()), [])


class StartedMatchProtectionTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_league()
        make_match(self.svc, "m1")
        self.a = self.svc.lock_candidate("m1", "r_zhou", "主任李")
        self.svc.confirm_assignment(self.a.id, "主任李")

    def test_started_but_unfinished_match_is_protected(self):
        self.svc.mark_started("m1")  # 进行中，尚未结束
        with self.assertRaises(HistoricalAssignmentProtected):
            self.svc.cancel_assignment(self.a.id, "开赛後想撤换", "主任李")
        with self.assertRaises(HistoricalAssignmentProtected):
            self.svc.substitute_takeover("m1", "r_wu", "开赛後换人", "主任李")
        # 新增冲突也不会作废进行中比赛的指派
        result = self.svc.declare_conflict("r_zhou", "t_sh", "family")
        self.assertEqual(result["reevaluated"]["superseded_assignment_ids"], [])
        self.assertIn("m1", result["reevaluated"]["started_or_finished_protected"])
        self.assertEqual(
            self.svc.assignment_timeline(self.a.id)["status"],
            AssignmentStatus.CONFIRMED.value,
        )


class UnavailabilityReevaluationTest(unittest.TestCase):
    def test_added_unavailability_supersedes_upcoming(self):
        svc = build_league()
        make_match(svc, "m1", start="2026-10-01T19:00", end="2026-10-01T21:00")
        a = svc.lock_candidate("m1", "r_zhou", "主任李")
        svc.confirm_assignment(a.id, "主任李")
        result = svc.add_unavailability(
            "r_zhou", "2026-10-01T18:00", "2026-10-01T22:00", "临时公务"
        )
        self.assertIn(a.id, result["reevaluated"]["superseded_assignment_ids"])
        self.assertEqual(
            svc.assignment_timeline(a.id)["status"],
            AssignmentStatus.SUPERSEDED.value,
        )


class ExportByDateTest(unittest.TestCase):
    def test_export_uses_local_date_across_midnight_and_timezone(self):
        svc = build_league()
        # 上海时间 9/20 23:30 开赛、跨午夜到 9/21 01:00
        make_match(
            svc, "m_cross", city="上海",
            start="2026-09-20T23:30", end="2026-09-21T01:00",
        )
        # 东京时间 9/21 20:00（= UTC 11:00 = 上海 19:00）—— 同一 UTC 时刻
        make_match(
            svc, "m_tokyo", city="上海",
            start="2026-09-21T20:00", end="2026-09-21T22:00",
            tz="Asia/Tokyo",
        )

        # 以亚洲/上海口径导出 9/20：只含跨天那场（按开赛当地日期）
        exp_20 = svc.export_by_date(date(2026, 9, 20), "Asia/Shanghai")
        ids_20 = [row["match"]["id"] for row in exp_20["matches"]]
        self.assertEqual(ids_20, ["m_cross"])
        cross = exp_20["matches"][0]["match"]
        self.assertTrue(cross["start_local"].startswith("2026-09-20T23:30"))
        self.assertTrue(cross["end_local"].startswith("2026-09-21T01:00"))

        # 同一场比赛按东京口径导出：开赛时刻在东京是 9/21 00:30
        exp_tokyo_21 = svc.export_by_date(date(2026, 9, 21), "Asia/Tokyo")
        ids_tokyo = [row["match"]["id"] for row in exp_tokyo_21["matches"]]
        self.assertIn("m_cross", ids_tokyo)
        self.assertIn("m_tokyo", ids_tokyo)

        # 上海口径 9/21：跨天场按开赛日仍归 9/20，东京场归 9/21（上海19:00）
        exp_sh_21 = svc.export_by_date(date(2026, 9, 21), "Asia/Shanghai")
        ids_sh21 = [row["match"]["id"] for row in exp_sh_21["matches"]]
        self.assertNotIn("m_cross", ids_sh21)
        self.assertIn("m_tokyo", ids_sh21)

    def test_export_includes_full_timeline(self):
        svc = build_league()
        make_match(svc, "m1")
        a = svc.lock_candidate("m1", "r_zhou", "主任李")
        svc.confirm_assignment(a.id, "主任李")
        exp = svc.export_by_date(date(2026, 9, 20))
        row = exp["matches"][0]
        self.assertEqual(row["assignments"][0]["id"], a.id)
        self.assertEqual(len(row["assignments"][0]["timeline"]), 2)


if __name__ == "__main__":
    unittest.main()
