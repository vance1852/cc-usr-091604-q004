"""时区与跨天时段：不同城市、不同时区下的占用判定必须一致且可追溯。"""

import unittest
from datetime import date, datetime, timezone

from app.service import AssignmentService
from app.testing import FakeClock

SH = "Asia/Shanghai"
NY = "America/New_York"


class TimezoneTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 9, 18, 8, 0, tzinfo=timezone.utc))
        self.service = AssignmentService(clock=self.clock)
        self.service.add_referee(
            referee_id="R1",
            name="裁判R1",
            level="NATIONAL",
            events={"5v5"},
            regions={"华东", "海外"},
            home_city="上海",
        )

    def add_match(self, match_id, *, city="上海", region="华东", tz=SH, start, end):
        return self.service.add_match(
            match_id=match_id,
            name=f"比赛{match_id}",
            level="SEMIFINAL",
            event="5v5",
            region=region,
            city=city,
            tz=tz,
            start=start,
            end=end,
            teams=("蓝鲸队", "猛虎队"),
        )

    def codes(self, match_id):
        report = self.service.generate_candidates(match_id)
        return report, {e.code for e in report.excluded if e.referee_id == "R1"}

    def test_cross_midnight_unavailability_blocks_late_match(self):
        # 每晚 22:00 到次日 02:00（跨天）不可用。
        self.service.add_daily_unavailability(
            "R1", "22:00", "02:00", tz=SH, days=[date(2026, 10, 1)], reason="夜间休息"
        )
        late = self.add_match(
            "T1", start=datetime(2026, 10, 1, 23, 0), end=datetime(2026, 10, 2, 0, 30)
        )
        early = self.add_match(
            "T2", start=datetime(2026, 10, 1, 19, 30), end=datetime(2026, 10, 1, 21, 30)
        )

        _, late_codes = self.codes(late.id)
        early_report, early_codes = self.codes(early.id)

        self.assertIn("UNAVAILABLE", late_codes)
        self.assertNotIn("UNAVAILABLE", early_codes)
        self.assertEqual([c.referee_id for c in early_report.candidates], ["R1"])

    def test_cross_timezone_overlap_is_detected(self):
        # 纽约 10 月 1 日 20:00（EDT）= 上海 10 月 2 日 08:00。
        self.service.add_unavailability(
            "R1",
            datetime(2026, 10, 2, 7, 0),
            datetime(2026, 10, 2, 9, 0),
            tz=SH,
            reason="体检",
        )
        ny_match = self.add_match(
            "T3",
            city="纽约",
            region="海外",
            tz=NY,
            start=datetime(2026, 10, 1, 20, 0),
            end=datetime(2026, 10, 1, 22, 0),
        )

        _, codes = self.codes(ny_match.id)
        self.assertIn("UNAVAILABLE", codes)

    def test_naive_local_time_matches_explicit_utc(self):
        naive = self.add_match(
            "T4", start=datetime(2026, 10, 1, 19, 0), end=datetime(2026, 10, 1, 21, 0)
        )
        aware = self.add_match(
            "T5",
            start=datetime(2026, 10, 1, 11, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(naive.start_utc, aware.start_utc)
        self.assertEqual(naive.end_utc, aware.end_utc)

    def test_export_groups_by_requested_timezone(self):
        # 同一场纽约夜赛：在纽约时区属于 10 月 1 日，在上海时区属于 10 月 2 日。
        match = self.add_match(
            "T6",
            city="纽约",
            region="海外",
            tz=NY,
            start=datetime(2026, 10, 1, 20, 0),
            end=datetime(2026, 10, 1, 22, 0),
        )

        ny_view = self.service.export_by_date(date(2026, 10, 1), NY)
        sh_view_prev = self.service.export_by_date(date(2026, 10, 1), SH)
        sh_view_next = self.service.export_by_date(date(2026, 10, 2), SH)

        self.assertEqual([r["match_id"] for r in ny_view["rows"]], [match.id])
        self.assertEqual(sh_view_prev["rows"], [])
        self.assertEqual([r["match_id"] for r in sh_view_next["rows"]], [match.id])
        self.assertEqual(
            sh_view_next["rows"][0]["start_local"][:16], "2026-10-02T08:00"
        )


if __name__ == "__main__":
    unittest.main()
