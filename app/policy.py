"""指派策略阈值：距离、跨城转场、连续工作与近期场次上限。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Policy:
    # 跨城转场：平均时速 + 场馆间最低休息分钟数
    travel_kmh: float = 80.0
    base_rest_minutes: int = 30
    # 相邻两场间隔不超过该分钟数即视为同一段连续工作
    block_join_minutes: int = 120
    max_consecutive_hours: float = 10.0
    # 近期场次：滚动窗口与窗口内（含本场）上限
    recent_window_hours: int = 24
    max_recent_games: int = 3

    def travel_minutes(self, distance_km: float) -> float:
        if distance_km == float("inf"):
            return float("inf")
        return distance_km / self.travel_kmh * 60.0

    def required_gap_minutes(self, distance_km: float) -> float:
        """两场之间至少需要的分钟数：路程时间 + 基本休息。"""
        return self.travel_minutes(distance_km) + self.base_rest_minutes
