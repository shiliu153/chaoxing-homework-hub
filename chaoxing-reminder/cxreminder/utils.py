"""通用小工具：时区、目录、时间格式化。"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

# 学习通返回的时间均为北京时间，兜底使用 UTC+8
_FALLBACK_TZ = timezone(timedelta(hours=8))


def get_tz(name: str = "Asia/Shanghai"):
    """获取 tzinfo；Windows 上若缺少 tzdata 则退回固定 UTC+8。"""
    if ZoneInfo is not None and name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return _FALLBACK_TZ


def now_in(tz_name: str = "Asia/Shanghai") -> datetime:
    """返回指定时区的“本地朴素时间”（不带 tzinfo），便于与站点时间直接比较。"""
    return datetime.now(get_tz(tz_name)).replace(tzinfo=None)


def ensure_dir(path: str) -> str:
    if path:
        os.makedirs(path, exist_ok=True)
    return path


def fmt_delta(delta: timedelta) -> str:
    """把时间差格式化成「x 天 x 小时 / x 小时 x 分 / x 分钟」。"""
    seconds = int(abs(delta.total_seconds()))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{max(minutes, 1)} 分钟"


def human_time(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "未知"
