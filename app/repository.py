"""线程安全的内存存储。

所有“检查-修改”复合操作都通过 :meth:`Repository.transaction` 串行化，
配合 :class:`~app.models.Match` 上的乐观锁版本号，保证并发确认时
只有一方能成功，且每次变更都可追溯。
"""

from __future__ import annotations

import itertools
import threading
from contextlib import contextmanager

from app.errors import NotFoundError
from app.models import Assignment, ConflictOfInterest, Match, Referee


class Repository:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.referees: dict[str, Referee] = {}
        self.matches: dict[str, Match] = {}
        self.assignments: dict[str, Assignment] = {}
        self.conflicts: dict[str, ConflictOfInterest] = {}
        self._seq = itertools.count(1)

    @contextmanager
    def transaction(self):
        """串行化一段复合操作；可重入，服务层公开方法各自持有。"""
        self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()

    def next_seq(self) -> int:
        """全局单调序号，给所有时间线事件一个全序。"""
        with self._lock:
            return next(self._seq)

    # ---- 实体存取 ----

    def get_referee(self, referee_id: str) -> Referee:
        try:
            return self.referees[referee_id]
        except KeyError:
            raise NotFoundError(f"裁判不存在: {referee_id}") from None

    def get_match(self, match_id: str) -> Match:
        try:
            return self.matches[match_id]
        except KeyError:
            raise NotFoundError(f"比赛不存在: {match_id}") from None

    def get_assignment(self, assignment_id: str) -> Assignment:
        try:
            return self.assignments[assignment_id]
        except KeyError:
            raise NotFoundError(f"指派不存在: {assignment_id}") from None

    def get_conflict(self, conflict_id: str) -> ConflictOfInterest:
        try:
            return self.conflicts[conflict_id]
        except KeyError:
            raise NotFoundError(f"冲突申报不存在: {conflict_id}") from None

    def assignments_of_match(self, match_id: str) -> list[Assignment]:
        return [a for a in self.assignments.values() if a.match_id == match_id]

    def assignments_of_referee(self, referee_id: str) -> list[Assignment]:
        return [a for a in self.assignments.values() if a.referee_id == referee_id]

    def active_conflicts_of(self, referee_id: str) -> list[ConflictOfInterest]:
        return [
            c for c in self.conflicts.values() if c.referee_id == referee_id and c.active
        ]
