"""候选名单生成：资格过滤、打分排序、空结果原因区分。"""

import unittest
from datetime import datetime, timezone

from app.models import AssignmentStatus
from app.service import AssignmentService
from app.testing import FakeClock, book_and_confirm

CST = "Asia/Shanghai"


class CandidateTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 9, 18, 8, 0, tzinfo=timezone.utc))
        self.service = AssignmentService(clock=self.clock)

    def add_referee(self, referee_id, **overrides):
        params = {
            "name": f"裁判{referee_id}",
            "level": "NATIONAL",
            "events": {"5v5"},
            "regions": {"华东"},
            "home_city": "上海",
        }
        params.update(overrides)
        return self.service.add_referee(referee_id=referee_id, **params)

    def add_semifinal(self, match_id="M1", **overrides):
        params = {
            "name": "半决赛第一场",
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

    def test_qualification_filters_carry_codes_and_categories(self):
        self.add_referee("R1")  # 国家级，全部符合
        self.add_referee("R2", level="LEVEL_1")  # 等级不足
        self.add_referee("R3", level="LEVEL_2", events={"3v3"}, regions={"华北"}, home_city="北京")
        match = self.add_semifinal()

        report = self.service.generate_candidates(match.id)

        self.assertIsNone(report.empty_reason)
        self.assertEqual([c.referee_id for c in report.candidates], ["R1"])
        by_ref = {}
        for exclusion in report.excluded:
            by_ref.setdefault(exclusion.referee_id, []).append(exclusion)
        self.assertEqual({e.code for e in by_ref["R2"]}, {"LEVEL_TOO_LOW"})
        self.assertEqual(
            {e.code for e in by_ref["R3"]},
            {"LEVEL_TOO_LOW", "EVENT_NOT_COVERED", "REGION_NOT_COVERED"},
        )
        self.assertTrue(all(e.category == "qualification" for e in report.excluded))

    def test_empty_reason_no_qualified_candidates(self):
        self.add_referee("R1", level="LEVEL_1")
        self.add_referee("R2", level="LEVEL_2", events={"3v3"})
        match = self.add_semifinal()

        report = self.service.generate_candidates(match.id)

        self.assertEqual(report.candidates, [])
        self.assertEqual(report.empty_reason, "NO_QUALIFIED_CANDIDATES")
        self.assertNotIn("conflict", {e.category for e in report.excluded})

    def test_empty_reason_all_excluded_by_conflict(self):
        self.add_referee("R1")
        self.add_referee("R2", home_city="杭州")
        self.add_referee("R3", level="LEVEL_1")  # 资格不符，与冲突无关
        match = self.add_semifinal()
        self.service.declare_conflict("R1", "蓝鲸队", "TRAINING", "暑期训练营讲师")
        self.service.declare_conflict("R2", "猛虎队", "RELATIVE", "配偶为球队领队")

        report = self.service.generate_candidates(match.id)

        self.assertEqual(report.candidates, [])
        self.assertEqual(report.empty_reason, "ALL_EXCLUDED_BY_CONFLICT")
        conflict_exclusions = [e for e in report.excluded if e.category == "conflict"]
        self.assertEqual({e.referee_id for e in conflict_exclusions}, {"R1", "R2"})
        self.assertTrue(all(e.code == "CONFLICT_OF_INTEREST" for e in conflict_exclusions))

    def test_scoring_prefers_close_and_rested_referee(self):
        near = self.add_referee("R1", home_city="上海")
        far = self.add_referee("R2", home_city="北京")
        busy = self.add_referee("R3", home_city="上海")
        # R3 近期刚执法过一场：同城但近期场次多，排名应落后于 R1。
        earlier = self.service.add_match(
            name="小组赛",
            level="GROUP",
            event="5v5",
            region="华东",
            city="上海",
            tz=CST,
            start=datetime(2026, 9, 30, 19, 0),
            end=datetime(2026, 9, 30, 21, 0),
            teams=("甲队", "乙队"),
        )
        book_and_confirm(self.service, earlier.id, "R3")
        match = self.add_semifinal()

        report = self.service.generate_candidates(match.id)

        self.assertEqual([c.referee_id for c in report.candidates], ["R1", "R3", "R2"])
        by_id = {c.referee_id: c for c in report.candidates}
        self.assertEqual(by_id["R1"].distance_km, 0.0)
        self.assertGreater(by_id["R2"].distance_km, 1000)
        self.assertEqual(by_id["R3"].recent_matches, 1)
        self.assertEqual(by_id["R1"].recent_matches, 0)
        self.assertIn("weights", by_id["R1"].breakdown)

    def test_unavailable_window_excludes(self):
        self.add_referee("R1")
        self.service.add_unavailability(
            "R1",
            datetime(2026, 10, 1, 18, 0),
            datetime(2026, 10, 1, 20, 0),
            tz=CST,
            reason="家中事务",
        )
        match = self.add_semifinal()

        report = self.service.generate_candidates(match.id)

        self.assertEqual(report.candidates, [])
        self.assertEqual(report.excluded[0].code, "UNAVAILABLE")
        self.assertEqual(report.excluded[0].category, "availability")
        self.assertIn("家中事务", report.excluded[0].detail)

    def test_double_booking_excludes(self):
        self.add_referee("R1")
        first = self.service.add_match(
            name="小组赛夜场",
            level="GROUP",
            event="5v5",
            region="华东",
            city="上海",
            tz=CST,
            start=datetime(2026, 10, 1, 19, 30),
            end=datetime(2026, 10, 1, 21, 30),
            teams=("甲队", "乙队"),
        )
        book_and_confirm(self.service, first.id, "R1")
        match = self.add_semifinal()  # 19:00-21:00 与上场重叠

        report = self.service.generate_candidates(match.id)

        self.assertEqual(report.candidates, [])
        self.assertEqual(report.excluded[0].code, "ALREADY_BOOKED")

    def test_cross_city_travel_gap_excludes(self):
        self.add_referee("R1", regions={"华东", "华北"}, home_city="北京")
        morning = self.service.add_match(
            name="北京上午场",
            level="GROUP",
            event="5v5",
            region="华北",
            city="北京",
            tz=CST,
            start=datetime(2026, 10, 1, 10, 0),
            end=datetime(2026, 10, 1, 12, 0),
            teams=("甲队", "乙队"),
        )
        book_and_confirm(self.service, morning.id, "R1")
        # 天津 13:00 开赛：与北京场仅隔 60 分钟，跨城缓冲不足 → 连续跨城赶场被拦截。
        tianjin = self.service.add_match(
            name="天津下午场",
            level="GROUP",
            event="5v5",
            region="华北",
            city="天津",
            tz=CST,
            start=datetime(2026, 10, 1, 13, 0),
            end=datetime(2026, 10, 1, 15, 0),
            teams=("丙队", "丁队"),
        )
        # 同城 13:00 开赛：同城缓冲 30 分钟，60 分钟间隔足够。
        beijing = self.service.add_match(
            name="北京下午场",
            level="GROUP",
            event="5v5",
            region="华北",
            city="北京",
            tz=CST,
            start=datetime(2026, 10, 1, 13, 0),
            end=datetime(2026, 10, 1, 15, 0),
            teams=("戊队", "己队"),
        )

        cross_city = self.service.generate_candidates(tianjin.id)
        same_city = self.service.generate_candidates(beijing.id)

        self.assertEqual(cross_city.candidates, [])
        self.assertEqual(cross_city.excluded[0].code, "INSUFFICIENT_TRAVEL_GAP")
        self.assertIn("跨城", cross_city.excluded[0].detail)
        self.assertEqual([c.referee_id for c in same_city.candidates], ["R1"])

    def test_daily_continuous_limit_excludes(self):
        self.add_referee("R1")
        # 已确认 08:00-10:00 与 10:30-12:00 两场（间隔 30 分钟，构成连续工作块）。
        for idx, (start, end) in enumerate(
            [((8, 0), (10, 0)), ((10, 30), (12, 0))], start=1
        ):
            match = self.service.add_match(
                name=f"上午场{idx}",
                level="GROUP",
                event="5v5",
                region="华东",
                city="上海",
                tz=CST,
                start=datetime(2026, 10, 1, *start),
                end=datetime(2026, 10, 1, *end),
                teams=(f"队{idx}A", f"队{idx}B"),
            )
            book_and_confirm(self.service, match.id, "R1")
        # 候选场 12:30-14:00：串联后连续工作 08:00-14:00 共 360 分钟，超过 240 分钟上限。
        match = self.add_semifinal(
            start=datetime(2026, 10, 1, 12, 30), end=datetime(2026, 10, 1, 14, 0)
        )

        report = self.service.generate_candidates(match.id)

        self.assertEqual(report.candidates, [])
        self.assertEqual(report.excluded[0].code, "DAILY_LIMIT_EXCEEDED")
        self.assertIn("360", report.excluded[0].detail)

    def test_limit_parameter_and_report_serialization(self):
        for idx in range(1, 4):
            self.add_referee(f"R{idx}")
        match = self.add_semifinal()

        report = self.service.generate_candidates(match.id, limit=2)

        self.assertEqual(len(report.candidates), 2)
        data = report.to_dict()
        self.assertEqual(data["match_id"], match.id)
        self.assertIsNone(data["empty_reason"])
        self.assertEqual(len(data["candidates"]), 2)


if __name__ == "__main__":
    unittest.main()
