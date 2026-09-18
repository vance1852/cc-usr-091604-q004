"""按日期导出：JSON/CSV 两种格式，历史指派照常可查。"""

import csv
import io
import unittest
from datetime import date, datetime, timezone

from app.models import MatchStatus
from app.service import AssignmentService
from app.testing import FakeClock, book_and_confirm

CST = "Asia/Shanghai"
DIRECTOR = "竞赛主任"


class ExportTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 9, 18, 8, 0, tzinfo=timezone.utc))
        self.service = AssignmentService(clock=self.clock)
        self.service.add_referee(
            referee_id="R1",
            name="王强",
            level="NATIONAL",
            events={"5v5"},
            regions={"华东"},
            home_city="上海",
        )
        self.match1 = self.service.add_match(
            match_id="E1",
            name="半决赛第一场",
            level="SEMIFINAL",
            event="5v5",
            region="华东",
            city="上海",
            tz=CST,
            start=datetime(2026, 10, 1, 19, 0),
            end=datetime(2026, 10, 1, 21, 0),
            teams=("蓝鲸队", "猛虎队"),
        )
        self.match2 = self.service.add_match(
            match_id="E2",
            name="半决赛第二场",
            level="SEMIFINAL",
            event="5v5",
            region="华东",
            city="上海",
            tz=CST,
            start=datetime(2026, 10, 1, 21, 30),
            end=datetime(2026, 10, 1, 23, 30),
            teams=("飞鹰队", "猎豹队"),
        )
        self.match3 = self.service.add_match(
            match_id="E3",
            name="决赛",
            level="FINAL",
            event="5v5",
            region="华东",
            city="上海",
            tz=CST,
            start=datetime(2026, 10, 3, 19, 0),
            end=datetime(2026, 10, 3, 21, 0),
            teams=("待定A", "待定B"),
        )
        assignment = book_and_confirm(
            self.service, self.match1.id, "R1", director=DIRECTOR
        )[0]
        self.service.respond(
            assignment.id, referee_id="R1", accept=True, reason="确认执法"
        )

    def test_json_export_only_includes_requested_date(self):
        result = self.service.export_by_date(date(2026, 10, 1), CST)

        self.assertEqual(result["date"], "2026-10-01")
        self.assertEqual(result["tz"], CST)
        self.assertEqual(len(result["rows"]), 2)  # E1 有指派，E2 空缺
        staffed = next(r for r in result["rows"] if r["match_id"] == "E1")
        self.assertEqual(staffed["referee_name"], "王强")
        self.assertEqual(staffed["assignment_status"], "已接受")
        self.assertEqual(staffed["last_event"], "ACCEPTED")
        self.assertEqual(staffed["last_event_reason"], "确认执法")
        self.assertEqual(staffed["match_level"], "半决赛")
        unstaffed = next(r for r in result["rows"] if r["match_id"] == "E2")
        self.assertEqual(unstaffed["assignment_status"], "UNASSIGNED")
        self.assertEqual(unstaffed["referee_id"], "")

    def test_csv_export_is_parseable(self):
        text = self.service.export_by_date("2026-10-01", CST, fmt="csv")

        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["match_id"], "E1")
        self.assertEqual(rows[0]["referee_name"], "王强")
        self.assertEqual(rows[1]["assignment_status"], "UNASSIGNED")

    def test_finished_match_assignments_remain_exportable(self):
        self.service.set_match_status(self.match1.id, MatchStatus.FINISHED, actor=DIRECTOR)

        result = self.service.export_by_date(date(2026, 10, 1), CST)

        staffed = next(r for r in result["rows"] if r["match_id"] == "E1")
        self.assertEqual(staffed["match_status"], "已结束")
        self.assertEqual(staffed["assignment_status"], "已接受")
        self.assertEqual(staffed["referee_name"], "王强")

    def test_export_accepts_string_date_and_empty_day(self):
        empty = self.service.export_by_date("2026-10-05", CST)
        self.assertEqual(empty["rows"], [])
        self.assertEqual(self.service.export_by_date("2026-10-05", CST, fmt="csv"), "")


if __name__ == "__main__":
    unittest.main()
