"""车机时区工具（时区 P0 规范）。

背景：上游 fork 的 Web 界面视频时间显示偏差 8 小时，根因是 mvhd 的 UTC 时间
按 Pi 系统时区转 naive 后与文件名的北京时间混用（见审计报告 AUDIT.md）。
本模块是全仓库唯一的时间转换出口：

- 绝不裸用 ``datetime.fromtimestamp()``（无 tz 参数的调用一律禁止）；
- 特斯拉文件名时间戳、视频显示时间一律按**车机本地时间**语义处理，
  不依赖 Pi 系统时区；
- 所有返回的 datetime 均为 aware（带车机时区）。

时区来源：仓库根 ``config.yaml`` 的 ``tesla_timezone``（默认 ``Asia/Shanghai``）。
"""

import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# 特斯拉录像文件名时间戳：2026-10-07_08-30-00（车机本地时间）
_FILENAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})")

_DEFAULT_TZ = "Asia/Shanghai"

_tz_cache = {}


def car_tz() -> ZoneInfo:
    """车机时区（读 config.yaml ``tesla_timezone``，默认 Asia/Shanghai）。

    时区名非法时回退 Asia/Shanghai 并记 warning，不抛异常。
    """
    # 延迟导入，避免 config ↔ tzutil 循环依赖
    from .config import get_tesla_timezone_name

    name = get_tesla_timezone_name() or _DEFAULT_TZ
    if name in _tz_cache:
        return _tz_cache[name]
    try:
        tz = ZoneInfo(name)
    except Exception:
        logger.warning("时区配置非法：%s，回退为 %s", name, _DEFAULT_TZ)
        tz = ZoneInfo(_DEFAULT_TZ)
    _tz_cache[name] = tz
    return tz


def parse_filename_time(filename: str) -> datetime | None:
    """从特斯拉文件名解析车机本地时间，返回 car_tz 的 aware datetime。

    文件名形如 ``2026-10-07_08-30-00-front.mp4``；语义为车机本地时间
    （不是 UTC，也不是 Pi 系统时间）。匹配不到或日期非法时返回 None。
    """
    if not filename:
        return None
    m = _FILENAME_RE.search(filename)
    if not m:
        return None
    try:
        dt = datetime.strptime(
            m.group(1) + "_" + m.group(2), "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=car_tz())


def format_car_time(dt: datetime) -> str:
    """格式化为车机本地时间字符串：``%Y-%m-%d %H:%M``。

    naive 输入视为车机本地时间；aware 输入先转车机时区。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=car_tz())
    else:
        dt = dt.astimezone(car_tz())
    return dt.strftime("%Y-%m-%d %H:%M")


def epoch_to_car(ts: float) -> datetime:
    """Unix 时间戳 → 车机本地 aware datetime（替代裸 ``fromtimestamp``）。"""
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(car_tz())


def now_car() -> datetime:
    """当前车机本地时间（aware）。"""
    return datetime.now(car_tz())


def car_to_epoch(dt: datetime) -> float:
    """车机本地时间 → Unix 时间戳。naive 输入视为车机本地时间。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=car_tz())
    return dt.timestamp()
