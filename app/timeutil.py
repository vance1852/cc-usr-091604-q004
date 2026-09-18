"""时间与距离工具。

平台内部统一使用 UTC 感知时间；所有对外入口允许传入带时区的本地时间，
在这里归一化。跨天时段（如 22:00 到次日 02:00）通过绝对时间区间自然表达。
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def ensure_utc(dt: datetime, tz: str | None = None) -> datetime:
    """把可能朴素的本地时间归一化为 UTC 感知时间。

    朴素时间必须显式给出 ``tz``，否则拒绝猜测，避免时区歧义。
    """
    if dt.tzinfo is None:
        if tz is None:
            raise ValueError("朴素时间必须提供 tz 参数以确定时区")
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(timezone.utc)


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """两个半开区间 [start, end) 是否重叠。"""
    return a_start < b_end and b_start < a_end


def gap_minutes(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> float:
    """两个不重叠区间之间的间隔分钟数（重叠时为 0）。"""
    if overlaps(a_start, a_end, b_start, b_end):
        return 0.0
    if a_end <= b_start:
        return (b_start - a_end).total_seconds() / 60.0
    return (a_start - b_end).total_seconds() / 60.0


def parse_hhmm(value: str) -> time:
    """解析 ``"HH:MM"`` 为 :class:`datetime.time`。"""
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def daily_window(day: date, start_hhmm: str, end_hhmm: str, tz: str) -> tuple[datetime, datetime]:
    """把某地的“每日时段”展开为绝对 UTC 区间。

    当结束时间不晚于开始时间时视为跨天时段，结束时间顺延到次日，
    例如 22:00–02:00 表示前一晚 22 点到次日凌晨 2 点。
    """
    zone = ZoneInfo(tz)
    start_local = datetime.combine(day, parse_hhmm(start_hhmm), tzinfo=zone)
    end_local = datetime.combine(day, parse_hhmm(end_hhmm), tzinfo=zone)
    if end_local <= start_local:
        end_local += timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两坐标间的大圆距离（公里）。"""
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))
