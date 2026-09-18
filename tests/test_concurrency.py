"""并发确认测试：证明乐观锁可追溯、同一裁判不会被重复占用。"""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.service import StateConflictError

from tests.fixtures import build_league, make_match


class ConcurrentConfirmTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_league()

    def test_only_one_confirm_succeeds_with_expected_version(self):
        make_match(
            self.svc, "m1", start="2026-09-20T19:00", end="2026-09-20T21:00"
        )
        assignment = self.svc.lock_candidate("m1", "r_zhou", "主任李")

        outcomes: list[str] = []
        barrier = threading.Barrier(4)

        def confirm(actor: str):
            barrier.wait()
            try:
                self.svc.confirm_assignment(
                    assignment.id, actor, expected_version=1
                )
                outcomes.append(f"{actor}:ok")
            except StateConflictError as exc:
                outcomes.append(f"{actor}:{exc.code}")

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(confirm, ["主任甲", "主任乙", "主任丙", "主任丁"]))

        oks = [o for o in outcomes if o.endswith(":ok")]
        conflicts = [o for o in outcomes if o.endswith("state_conflict")]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(conflicts), 3)
        # 只有一个确认者被记录
        record = self.svc.assignment_timeline(assignment.id)
        self.assertEqual(record["status"], "confirmed")
        confirmer = oks[0].split(":")[0]
        self.assertEqual(record["confirmed_by"], confirmer)
        self.assertEqual(record["lock_version"], 2)
        confirm_events = [e for e in record["timeline"] if e["action"] == "confirmed"]
        self.assertEqual(len(confirm_events), 1)

    def test_concurrent_locks_of_same_official_never_double_book(self):
        # 两场时间重叠的比赛，多个线程同时抢同一裁判
        make_match(self.svc, "m1", city="上海",
                   start="2026-09-20T19:00", end="2026-09-20T21:00")
        make_match(self.svc, "m2", city="上海",
                   start="2026-09-20T19:00", end="2026-09-20T21:00")
        results: dict[str, str] = {}
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def try_lock(match_id: str):
            barrier.wait()
            try:
                a = self.svc.lock_candidate(match_id, "r_zhou", "主任李")
                with lock:
                    results[match_id] = a.id
            except Exception as exc:  # noqa: BLE001
                with lock:
                    results[match_id] = exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(try_lock, ["m1", "m2"]))

        values = list(results.values())
        self.assertEqual(len([v for v in values if v.startswith("asn_")]), 1)
        # 另一场对同一裁判的锁定必然失败：可能是同一临界区内的状态冲突，
        # 也可能是排班规则的重复占用（double_booked → excluded_by_rules）
        failed = [v for v in values if not v.startswith("asn_")]
        self.assertEqual(len(failed), 1)
        self.assertIn(failed[0], ("state_conflict", "excluded_by_rules"))

        # 存储中该裁判在该时间窗内只有一条占用指派
        occupying = [
            a
            for a in self.svc.store.assignments.values()
            if a.official_id == "r_zhou" and a.status.occupies
        ]
        self.assertEqual(len(occupying), 1)


if __name__ == "__main__":
    unittest.main()
