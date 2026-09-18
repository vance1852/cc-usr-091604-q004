"""测试与演示辅助：可注入的时钟，保证时间相关行为可复现。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class FakeClock:
    """手动推进的时钟，作为 ``AssignmentService(clock=...)`` 的注入点。"""

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        self._now = moment


def book_and_confirm(service, match_id: str, referee_id: str, *, director: str = "主任"):
    """测试/演示辅助：锁定一名候选并立即确认，返回确认后的指派列表。"""
    token = service.lock_candidates(match_id, [referee_id], director=director)["lock_token"]
    return service.confirm(match_id, expected_version=token, director=director)
