"""内存存储：集中数据与全局锁，所有状态变更在锁内完成。"""

from __future__ import annotations

import itertools
import threading
from contextlib import contextmanager

from .geo import CityRegistry
from .models import (
    Assignment,
    ConflictDeclaration,
    Match,
    Official,
    Team,
    Unavailability,
)


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._seq = itertools.count(1)
        self.cities = CityRegistry.default()

        self.officials: dict[str, Official] = {}
        self.teams: dict[str, Team] = {}
        self.matches: dict[str, Match] = {}
        self.assignments: dict[str, Assignment] = {}
        self.unavailabilities: list[Unavailability] = []
        self.conflicts: list[ConflictDeclaration] = []

    @contextmanager
    def lock(self):
        with self._lock:
            yield

    def next_id(self, prefix: str) -> str:
        with self._lock:
            return f"{prefix}_{next(self._seq):04d}"
