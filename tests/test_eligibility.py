"""资格与候选评估：硬性不合格 vs 冲突/排班排除的区分。"""

import unittest

from app.eligibility import (
    REASON_CONFLICT,
    REASON_CONSECUTIVE,
    REASON_OCCUPIED,
    REASON_RECENT_LOAD,
    REASON_TRAVEL,
    REASON_UNAVAILABLE,
)
from app.models import Grade

from tests.fixtures import build_league, make_match


class CandidateOutcomeTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_league()
        self.match = make_match(self.svc)

    def _by_id(self, rows):
        return {row["official_id"]: row for row in rows}

    def test_baseline_ranks_qualified_and_reports_others_separately(self):
        res = self.svc.candidates("m1")
        # 合格候选：周宁（上海本地0km）与吴敏（杭州，覆盖上海）；周宁距离更近排第一
        self.assertEqual(res["summary"]["outcome"], "candidates_available")
        candidate_ids = [c["official_id"] for c in res["candidates"]]
        self.assertEqual(candidate_ids[0], "r_zhou")
        self.assertIn("r_wu", candidate_ids)
        excluded = self._by_id(res["excluded"])
        disqualified = self._by_id(res["disqualified"])
        # 郑海：等级不足（硬性不合格）
        self.assertIn("r_low", disqualified)
        codes = {r["code"] for r in disqualified["r_low"]["reasons"]}
        self.assertIn("grade_too_low", codes)
        # 排球裁判：项目不符（硬性不合格）
        self.assertIn("r_onlyvolley", disqualified)
        # 吴敏：杭州常驻且区域含上海，硬资格合格、进入候选
        # （上面只断言周宁排第一，吴敏是否候选取决于距离——两地都覆盖故也合格）
        self.assertNotIn("r_wu", disqualified)

    def test_no_qualified_pool_vs_all_excluded(self):
        # 一场在广州的乙级：唯一够级别的若区域不含广州 => 硬性不合格池为空
        svc = build_league()
        # 周宁区域含广州，吴敏不含；再让周宁不可用 => 全员被规则排除
        make_match(
            svc, "mg", level="乙级", city="广州",
            start="2026-09-25T19:00", end="2026-09-25T21:00",
        )
        svc.add_unavailability(
            "r_zhou", "2026-09-25T18:00", "2026-09-25T22:00", "出差"
        )
        res = svc.candidates("mg")
        self.assertEqual(res["summary"]["outcome"], "all_excluded_by_rules")
        only_excluded = {row["official_id"] for row in res["excluded"]}
        self.assertIn("r_zhou", only_excluded)
        reason_codes = {
            r["code"] for row in res["excluded"] for r in row["reasons"]
        }
        self.assertIn(REASON_UNAVAILABLE, reason_codes)

        # 改成所有裁判硬资格都不满足（比赛地为未覆盖城市）
        svc2 = build_league()
        svc2.store.cities.register("青岛", 36.0671, 120.3826)
        make_match(
            svc2, "mq", level="乙级", city="青岛",
            start="2026-09-25T19:00", end="2026-09-25T21:00",
        )
        res2 = svc2.candidates("mq")
        self.assertEqual(res2["summary"]["outcome"], "no_qualified_pool")
        self.assertEqual(res2["candidates"], [])
        self.assertEqual(res2["excluded"], [])
        self.assertTrue(res2["disqualified"])
        for row in res2["disqualified"]:
            self.assertTrue(
                any(r["code"] == "region_not_covered" for r in row["reasons"])
            )

    def test_declared_conflict_excludes_with_reason(self):
        self.svc.declare_conflict("r_zhou", "t_sh", "family", "儿子在该队")
        res = self.svc.candidates("m1")
        excluded = self._by_id(res["excluded"])
        self.assertIn("r_zhou", excluded)
        codes = [r["code"] for r in excluded["r_zhou"]["reasons"]]
        self.assertIn(REASON_CONFLICT, codes)
        conflict = next(r for r in excluded["r_zhou"]["reasons"] if r["code"] == REASON_CONFLICT)
        self.assertEqual(conflict["detail"]["team_id"], "t_sh")

    def test_explain_separates_interest_and_schedule_conflicts(self):
        # 先给周宁安排一场时间重叠的比赛（对阵双方不含 t_sh，避免冲突重评估波及它）
        other = make_match(
            self.svc, "m_other", start="2026-09-20T20:00", end="2026-09-20T22:00",
            city="杭州", home="t_gz", away="t_hz",
        )
        a = self.svc.lock_candidate("m_other", "r_zhou", "主任")
        self.svc.confirm_assignment(a.id, "主任")
        self.svc.declare_conflict("r_zhou", "t_sh", "training")

        explanation = self.svc.explain("m1", "r_zhou")
        self.assertFalse(explanation["assignable"])
        self.assertEqual(
            [c["code"] for c in explanation["interest_conflicts"]], [REASON_CONFLICT]
        )
        sched_codes = {c["code"] for c in explanation["schedule_conflicts"]}
        self.assertIn(REASON_OCCUPIED, sched_codes)

    def test_distance_drives_ranking(self):
        # 上海的比赛：周宁(上海0km) 应排在吴敏(杭州~170km)之前
        res = self.svc.candidates("m1")
        ids = [c["official_id"] for c in res["candidates"]]
        self.assertLess(ids.index("r_zhou"), ids.index("r_wu"))
        zhou = next(c for c in res["candidates"] if c["official_id"] == "r_zhou")
        self.assertEqual(zhou["distance_km"], 0.0)


class SchedulingRuleTest(unittest.TestCase):
    def test_cross_city_travel_gap_insufficient(self):
        svc = build_league()
        # 19:00-21:00 杭州的比赛先占用周宁，21:30 上海开赛，间隔仅 30 分钟
        make_match(
            svc, "mhz", city="杭州",
            start="2026-09-20T19:00", end="2026-09-20T21:00",
        )
        a = svc.lock_candidate("mhz", "r_zhou", "主任")
        svc.confirm_assignment(a.id, "主任")
        make_match(
            svc, "msh", city="上海",
            start="2026-09-20T21:30", end="2026-09-20T23:30",
        )
        res = svc.candidates("msh")
        excluded = {row["official_id"]: row for row in res["excluded"]}
        self.assertIn("r_zhou", excluded)
        codes = {r["code"] for r in excluded["r_zhou"]["reasons"]}
        self.assertIn(REASON_TRAVEL, codes)
        travel = next(r for r in excluded["r_zhou"]["reasons"] if r["code"] == REASON_TRAVEL)
        self.assertGreater(travel["detail"]["distance_km"], 100)

    def test_enough_travel_gap_passes(self):
        svc = build_league()
        make_match(
            svc, "mhz", city="杭州",
            start="2026-09-20T15:00", end="2026-09-20T17:00",
        )
        a = svc.lock_candidate("mhz", "r_zhou", "主任")
        svc.confirm_assignment(a.id, "主任")
        # 间隔 3 小时（180 分钟），足够杭州->上海（约170km ≈ 128 分钟 + 30 休息 ≈ 158 分钟）
        make_match(
            svc, "msh", city="上海",
            start="2026-09-20T20:00", end="2026-09-20T22:00",
        )
        res = svc.candidates("msh")
        ids = {c["official_id"] for c in res["candidates"]}
        self.assertIn("r_zhou", ids)

    def test_consecutive_hours_limit(self):
        from app.policy import Policy
        svc = build_league(policy=Policy(max_consecutive_hours=8.0, block_join_minutes=120))
        # 14:00-18:00（4h）+ 19:00-23:30（4.5h），间隔 1h 归为同一连续块 => 9.5h > 8h
        make_match(
            svc, "ma", city="上海",
            start="2026-09-20T14:00", end="2026-09-20T18:00",
        )
        a = svc.lock_candidate("ma", "r_zhou", "主任")
        svc.confirm_assignment(a.id, "主任")
        make_match(
            svc, "mb", city="上海",
            start="2026-09-20T19:00", end="2026-09-20T23:30",
        )
        res = svc.candidates("mb")
        excluded = {row["official_id"]: row for row in res["excluded"]}
        self.assertIn("r_zhou", excluded)
        codes = {r["code"] for r in excluded["r_zhou"]["reasons"]}
        self.assertIn(REASON_CONSECUTIVE, codes)

    def test_recent_load_limit(self):
        from app.policy import Policy
        svc = build_league(policy=Policy(max_recent_games=2, recent_window_hours=24))
        # 24 小时窗口内先安排 2 场，第 3 场应被 recent_load 排除
        starts = [
            ("m1", "2026-09-20T09:00", "2026-09-20T11:00"),
            ("m2", "2026-09-20T15:00", "2026-09-20T17:00"),
            ("m3", "2026-09-20T20:00", "2026-09-20T22:00"),
        ]
        for mid, s, e in starts[:2]:
            make_match(svc, mid, city="上海", start=s, end=e)
            a = svc.lock_candidate(mid, "r_zhou", "主任")
            svc.confirm_assignment(a.id, "主任")
        make_match(svc, "m3", city="上海", start=starts[2][1], end=starts[2][2])
        res = svc.candidates("m3")
        excluded = {row["official_id"]: row for row in res["excluded"]}
        self.assertIn("r_zhou", excluded)
        codes = {r["code"] for r in excluded["r_zhou"]["reasons"]}
        self.assertIn(REASON_RECENT_LOAD, codes)


if __name__ == "__main__":
    unittest.main()
