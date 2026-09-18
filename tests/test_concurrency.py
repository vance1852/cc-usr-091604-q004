"""并发确认：同一时刻只有一份确认生效，且全程留痕。"""

import threading
import unittest
from datetime import datetime, timezone

from app.errors import InvalidStateError, VersionConflictError
from app.models import AssignmentStatus
from app.service import AssignmentService
from app.testing import FakeClock

CST = "Asia/Shanghai"
DIRECTOR = "竞赛主任"


class ConcurrencyTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 9, 18, 8, 0, tzinfo=timezone.utc))
        self.service = AssignmentService(clock=self.clock)
        self.service.add_referee(
            referee_id="R1",
            name="裁判R1",
            level="NATIONAL",
            events={"5v5"},
            regions={"华东"},
            home_city="上海",
        )
        self.match = self.service.add_match(
            match_id="M1",
            name="半决赛",
            level="SEMIFINAL",
            event="5v5",
            region="华东",
            city="上海",
            tz=CST,
            start=datetime(2026, 10, 1, 19, 0),
            end=datetime(2026, 10, 1, 21, 0),
            teams=("蓝鲸队", "猛虎队"),
        )

    def _run_threads(self, count, target):
        barrier = threading.Barrier(count)
        results, errors = [], []

        def worker():
            barrier.wait(timeout=5)
            try:
                results.append(target())
            except Exception as exc:  # noqa: BLE001 - 测试需要收集全部异常
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        return results, errors

    def test_concurrent_confirm_only_one_wins(self):
        token = self.service.lock_candidates(self.match.id, ["R1"], director=DIRECTOR)[
            "lock_token"
        ]

        results, errors = self._run_threads(
            8,
            lambda: self.service.confirm(
                self.match.id, expected_version=token, director=DIRECTOR
            ),
        )

        self.assertEqual(len(results), 1, "并发确认只能成功一次")
        self.assertEqual(len(errors), 7)
        self.assertTrue(all(isinstance(e, VersionConflictError) for e in errors))
        assignments = self.service.match_assignments(self.match.id)
        confirmed = [a for a in assignments if a.status == AssignmentStatus.CONFIRMED]
        self.assertEqual(len(confirmed), 1)
        actions = [e.action for e in self.service.timeline(confirmed[0].id)]
        self.assertEqual(actions, ["LOCKED", "CONFIRMED"])
        self.assertEqual(self.service.repo.get_match(self.match.id).version, token + 1)

    def test_concurrent_respond_only_one_wins(self):
        token = self.service.lock_candidates(self.match.id, ["R1"], director=DIRECTOR)[
            "lock_token"
        ]
        assignment = self.service.confirm(
            self.match.id, expected_version=token, director=DIRECTOR
        )[0]

        barrier = threading.Barrier(2)
        outcomes, failures = [], []

        def race(accept):
            barrier.wait(timeout=5)
            try:
                outcomes.append(
                    self.service.respond(
                        assignment.id,
                        referee_id="R1",
                        accept=accept,
                        reason="接受" if accept else "拒绝",
                    )
                )
            except InvalidStateError as exc:
                failures.append(exc)

        threads = [
            threading.Thread(target=race, args=(True,)),
            threading.Thread(target=race, args=(False,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(outcomes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIn(
            outcomes[0].status,
            (AssignmentStatus.ACCEPTED, AssignmentStatus.DECLINED),
        )
        actions = [e.action for e in self.service.timeline(assignment.id)]
        self.assertEqual(len(actions), 3)  # LOCKED / CONFIRMED / 唯一一次响应

    def test_lock_after_lock_invalidates_previous_token(self):
        first = self.service.lock_candidates(self.match.id, ["R1"], director=DIRECTOR)
        second = self.service.lock_candidates(self.match.id, ["R1"], director=DIRECTOR)
        with self.assertRaises(VersionConflictError):
            self.service.confirm(
                self.match.id, expected_version=first["lock_token"], director=DIRECTOR
            )
        confirmed = self.service.confirm(
            self.match.id, expected_version=second["lock_token"], director=DIRECTOR
        )
        self.assertEqual(confirmed[0].status, AssignmentStatus.CONFIRMED)
        # 第一次锁定被 UNLOCK 撤销，时间线完整保留两次操作痕迹。
        revoked = [
            a
            for a in self.service.match_assignments(self.match.id)
            if a.status == AssignmentStatus.REVOKED
        ]
        self.assertEqual(len(revoked), 1)
        self.assertEqual(
            [e.action for e in self.service.timeline(revoked[0].id)],
            ["LOCKED", "UNLOCK"],
        )


if __name__ == "__main__":
    unittest.main()
