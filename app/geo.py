"""城市坐标与球面距离（haversine），用于执法区域与跨城赶路时间估算。"""

from __future__ import annotations

import math
from dataclasses import dataclass

# 常见城市经纬度，register_city 可继续补充
_DEFAULT_CITIES: dict[str, tuple[float, float]] = {
    "北京": (39.9042, 116.4074),
    "上海": (31.2304, 121.4737),
    "广州": (23.1291, 113.2644),
    "深圳": (22.5431, 114.0579),
    "成都": (30.5728, 104.0668),
    "杭州": (30.2741, 120.1551),
    "武汉": (30.5928, 114.3055),
    "南京": (32.0603, 118.7969),
    "西安": (34.3416, 108.9398),
    "重庆": (29.5630, 106.5516),
    "天津": (39.3434, 117.3616),
    "苏州": (31.2989, 120.5853),
}

EARTH_RADIUS_KM = 6371.0088


@dataclass
class CityRegistry:
    cities: dict[str, tuple[float, float]]

    @classmethod
    def default(cls) -> "CityRegistry":
        return cls(dict(_DEFAULT_CITIES))

    def register(self, name: str, lat: float, lng: float) -> None:
        self.cities[name] = (lat, lng)

    def has(self, name: str) -> bool:
        return name in self.cities

    def distance_km(self, city_a: str, city_b: str) -> float:
        """两城市间的大圆距离（公里）；同一城市为 0；未知城市返回 inf。"""
        if city_a == city_b:
            return 0.0
        coords = self.cities
        if city_a not in coords or city_b not in coords:
            return math.inf
        lat1, lng1 = map(math.radians, coords[city_a])
        lat2, lng2 = map(math.radians, coords[city_b])
        dlng = lng2 - lng1
        dlat = lat2 - lat1
        h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
        return round(2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h)), 2)
