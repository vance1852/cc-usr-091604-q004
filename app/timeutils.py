"""时间工具：统一使用带时区信息的 datetime，内部以 UTC 语义比较。"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def local_datetime(
    y: int,
    m: int,
    d: int,
    hour: int = 0,
    minute: int = 0,
    tz_name: str = "Asia/Shanghai",
) -> datetime:
    """按当地挂钟时间构造带时区的 datetime。"""
    return datetime(y, m, d, hour, minute, tzinfo=ZoneInfo(tz_name))


def parse_dt(value: str | datetime, tz_name: str | None = None) -> datetime:
    """解析 ISO 字符串。

    - 字符串自带偏移量（如 ``2026-03-01T19:30+09:00``）时直接采用；
    - 只有“朴素”挂钟时间时，用 ``tz_name`` 指定的时区本地化。
    """
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        if tz_name is None:
            raise ValueError("朴素时间必须提供 tz 时区名")
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt


def to_iso(dt: datetime) -> str:
    return dt.isoformat()


def local_date_of(dt: datetime, tz_name: str) -> date:
    """带时区的时刻在指定时区下的当地日期（处理跨天）。"""
    return dt.astimezone(ZoneInfo(tz_name)).date()


def combine_local(d: date, t: time, tz_name: str) -> datetime:
    return datetime.combine(d, t, tzinfo=ZoneInfo(tz_name))
