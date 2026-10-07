"""提示音定时计划 + 随机分组服务（合并精简版）。

移植来源：
- fork ``scripts/web/services/chime_scheduler_service.py``（1433 行）
- fork ``scripts/web/services/chime_group_service.py``（450 行）

取舍说明：
- 保留：四类计划（weekly/date/holiday/recurring）的 CRUD、冲突校验、
  ``get_active_chime(check_time)`` 的优先级判定（节日 > 指定日期 > 每周）与
  "昨天回退"逻辑、``_select_random_chime`` 避开当前提示音、recurring 的
  on_boot/间隔执行判定（should_execute/record_execution）、
  分组 CRUD 与成员管理、随机源分组、开机随机选择（apply_boot_random_chime）。
- 砍掉：与 Web 蓝图/DB/present-edit 双模式/USB gadget/分区挂载强耦合的全部代码。
  状态改存 JSON：``state/schedules.json``、``state/chime_groups.json``
  （随机配置并入 chime_groups.json 的 "random" 段，不再单独存文件）。
- 节日：中国节日（元旦/春节/清明/劳动节/端午/中秋/国庆/圣诞）。
  农历节日（春节/端午/中秋）用"年份 -> 公历日期"固定映射表简化实现，
  为近似值、不含官方调休；清明在 4 月 4-6 日浮动，简化固定为 4 月 5 日。
  超出映射表覆盖年份的，该节日当年不参与判定。
- 时区 P0：所有"现在"与日期判定一律经 ``web.services.tzutil``
  （``now_car()``/``car_tz()``）；传入的 naive datetime 一律视为车机本地时间，
  绝不裸用 ``datetime.now()``/``fromtimestamp()``。
- 随机数：去掉上游"每次用微秒时间戳重置种子"的做法，改用模块级 ``random``，
  保证可测试性（测试可 seed）；生产环境随机性不受影响。

用户可见消息一律中文；仅依赖标准库。
"""

import json
import logging
import os
import random
from datetime import datetime, time as dtime, timedelta

from .config import get_state_dir
from .tzutil import car_tz, now_car

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 中国节日（holiday 类型计划用）
# ---------------------------------------------------------------------------

# 固定公历节日：名称 -> (月, 日)
FIXED_HOLIDAYS = {
    "元旦": (1, 1),
    "清明": (4, 5),    # 清明在 4 月 4-6 日浮动，此处简化固定为 4 月 5 日
    "劳动节": (5, 1),
    "国庆": (10, 1),
    "圣诞": (12, 25),
}

# 农历节日：名称 -> {年份: (月, 日)}，为公历近似日期（不含官方调休）。
# 超出覆盖年份的，该节日当年不参与判定；如需扩展可按农历追加。
LUNAR_HOLIDAYS = {
    "春节": {
        2024: (2, 10), 2025: (1, 29), 2026: (2, 17), 2027: (2, 6),
        2028: (1, 26), 2029: (2, 13), 2030: (2, 3), 2031: (1, 23),
        2032: (2, 11),
    },
    "端午": {
        2024: (6, 10), 2025: (5, 31), 2026: (6, 19), 2027: (6, 9),
        2028: (5, 28), 2029: (6, 16),
    },
    "中秋": {
        2024: (9, 17), 2025: (10, 6), 2026: (9, 25), 2027: (9, 15),
        2028: (10, 3), 2029: (9, 22), 2030: (9, 12),
    },
}

ALL_HOLIDAYS = sorted(set(FIXED_HOLIDAYS) | set(LUNAR_HOLIDAYS))

# ---------------------------------------------------------------------------
# 计划类型相关常量
# ---------------------------------------------------------------------------

# 星期几：Python weekday() 0=周一 … 6=周日
DAYS_OF_WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday",
                "Friday", "Saturday", "Sunday"]

# 中文星期名 -> 英文（Web UI 传中文时做归一化）
_CN_DAYS = {
    "周一": "Monday", "星期一": "Monday",
    "周二": "Tuesday", "星期二": "Tuesday",
    "周三": "Wednesday", "星期三": "Wednesday",
    "周四": "Thursday", "星期四": "Thursday",
    "周五": "Friday", "星期五": "Friday",
    "周六": "Saturday", "星期六": "Saturday",
    "周日": "Sunday", "星期日": "Sunday", "周天": "Sunday", "星期天": "Sunday",
}

SCHEDULE_TYPES = ("weekly", "date", "holiday", "recurring")

RECURRING_INTERVALS = {
    "on_boot": "每次启动时",
    "15min": "每 15 分钟",
    "30min": "每 30 分钟",
    "1hour": "每小时",
    "2hour": "每 2 小时",
    "4hour": "每 4 小时",
    "6hour": "每 6 小时",
    "12hour": "每 12 小时",
}

INTERVAL_TO_MINUTES = {
    "15min": 15, "30min": 30, "1hour": 60, "2hour": 120,
    "4hour": 240, "6hour": 360, "12hour": 720,
}

# UI 约定的特殊返回值：需要用户确认"停用其他计划"时返回该标记
CONFIRM_DISABLE_OTHERS = "CONFIRM_DISABLE_OTHERS"


# ---------------------------------------------------------------------------
# 时间工具（车机时区，P0 约定）
# ---------------------------------------------------------------------------

def _car_naive(dt=None):
    """把 datetime 统一为车机本地的 naive 时间。

    - None -> 当前车机时间；
    - aware -> 转到车机时区再去 tzinfo；
    - naive -> 直接视为车机本地时间。
    """
    if dt is None:
        return now_car().replace(tzinfo=None)
    if dt.tzinfo is not None:
        return dt.astimezone(car_tz()).replace(tzinfo=None)
    return dt


def _parse_last_run(value):
    """解析 last_run（ISO 字符串），返回车机本地 naive 时间；解析失败返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return _car_naive(dt)


def _normalize_days(days):
    """星期列表归一化为英文名并按周一->周日排序；非法值抛 ValueError。"""
    normalized = []
    for day in days or []:
        name = _CN_DAYS.get(str(day).strip(), str(day).strip())
        if name not in DAYS_OF_WEEK:
            raise ValueError(f"非法的星期：{day}")
        if name not in normalized:
            normalized.append(name)
    if not normalized:
        raise ValueError("每周计划至少需要选择一天")
    return sorted(normalized, key=DAYS_OF_WEEK.index)


def _parse_hhmm(value):
    """解析 HH:MM，返回 dtime；非法抛 ValueError。"""
    try:
        parts = str(value).split(":")
        hour, minute = int(parts[0]), int(parts[1])
        if len(parts) != 2 or not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return dtime(hour, minute)
    except (ValueError, IndexError, AttributeError):
        raise ValueError(f"时间格式非法（应为 HH:MM）：{value}")


def _parse_month_day(value):
    """解析日期：接受 'MM-DD' 字符串 / (月, 日) 元组 / {'month':..,'day':..}。"""
    month = day = None
    if isinstance(value, str):
        parts = value.split("-")
        if len(parts) == 2:
            month, day = int(parts[0]), int(parts[1])
    elif isinstance(value, dict):
        month, day = value.get("month"), value.get("day")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        month, day = value
    if month is None or day is None:
        raise ValueError(f"日期格式非法（应为 MM-DD）：{value}")
    month, day = int(month), int(day)
    if not 1 <= month <= 12:
        raise ValueError(f"月份非法：{month}")
    try:
        datetime(2024, month, day)  # 闰年校验，允许 2 月 29 日
    except ValueError:
        raise ValueError(f"日期非法：{month}-{day}")
    return month, day


def _holidays_for_date(year, month, day):
    """返回落在指定公历日期的中国节日名列表。"""
    names = []
    for name, (m, d) in FIXED_HOLIDAYS.items():
        if m == month and d == day:
            names.append(name)
    for name, table in LUNAR_HOLIDAYS.items():
        md = table.get(year)
        if md and md[0] == month and md[1] == day:
            names.append(name)
    return names


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(f"读取状态文件失败（{path}）：{exc}")
        return default


def _save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        return True
    except OSError as exc:
        logger.error(f"保存状态文件失败（{path}）：{exc}")
        return False


# ---------------------------------------------------------------------------
# 定时计划
# ---------------------------------------------------------------------------

class ChimeScheduler:
    """管理锁车提示音定时计划。状态存 state/schedules.json。"""

    def __init__(self, schedule_file=None):
        self.schedule_file = schedule_file or os.path.join(get_state_dir(), "schedules.json")
        data = _load_json(self.schedule_file, [])
        self.schedules = data if isinstance(data, list) else []

    # -- 持久化 ------------------------------------------------------------
    def _save(self):
        return _save_json(self.schedule_file, self.schedules)

    # -- 查询 --------------------------------------------------------------
    def list_schedules(self, enabled_only=False):
        """列出全部计划（按时间排序）。"""
        items = self.schedules if not enabled_only else [
            s for s in self.schedules if s.get("enabled", True)]
        return sorted(items, key=lambda s: s.get("time", "00:00"))

    def get_schedule(self, schedule_id):
        return next((s for s in self.schedules if s.get("id") == schedule_id), None)

    def get_holidays_list(self):
        """可用的中国节日名列表（供 UI 下拉）。"""
        return list(ALL_HOLIDAYS)

    def get_recurring_intervals(self):
        return dict(RECURRING_INTERVALS)

    # -- 创建 --------------------------------------------------------------
    def create_schedule(self, data):
        """创建计划。

        data 字段：name / type(weekly|date|holiday|recurring) / chime(文件名，
        recurring 固定为 'RANDOM') / time(HH:MM，非 recurring) /
        days(weekly 用，英文或中文星期名列表) / date(date 用，'MM-DD') /
        holiday(holiday 用，中文节日名) / interval(recurring 用) /
        enabled(默认 True)。
        返回 (成功与否, 中文消息, 计划 id|None)。
        """
        data = dict(data or {})
        schedule_type = data.get("type", "weekly")
        if schedule_type not in SCHEDULE_TYPES:
            return False, f"计划类型非法：{schedule_type}", None

        # recurring 与其他计划互斥（沿用上游语义）
        enabled = bool(data.get("enabled", True))
        if enabled:
            has_recurring, existing = self._has_enabled_recurring()
            if schedule_type == "recurring":
                if has_recurring:
                    return False, (
                        f"已有启用的循环计划「{existing.get('name')}」，"
                        "同一时间只能启用一个循环计划。"), None
                others = [s for s in self.list_schedules(enabled_only=True)
                          if s.get("schedule_type") != "recurring"]
                if others:
                    return False, CONFIRM_DISABLE_OTHERS, None
            elif has_recurring:
                return False, (
                    f"循环计划「{existing.get('name')}」启用中，"
                    "请先停用它再添加定时计划。"), None

        chime = data.get("chime") or "RANDOM"
        try:
            schedule = self._build_schedule(schedule_type, data, chime)
        except ValueError as exc:
            return False, str(exc), None

        conflict = self._find_conflict(schedule)
        if conflict:
            return False, f"与已有计划「{conflict.get('name')}」时间冲突。", None

        schedule["id"] = max([s.get("id", 0) for s in self.schedules] + [0]) + 1
        schedule["created_at"] = now_car().isoformat()
        self.schedules.append(schedule)
        if self._save():
            logger.info(f"已创建{schedule_type}计划「{schedule['name']}」")
            return True, "计划创建成功", schedule["id"]
        self.schedules.pop()
        return False, "保存计划失败", None

    def _build_schedule(self, schedule_type, data, chime):
        name = (data.get("name") or "").strip()
        schedule = {
            "name": name,
            "chime_filename": chime,
            "schedule_type": schedule_type,
            "enabled": bool(data.get("enabled", True)),
        }
        if schedule_type == "recurring":
            interval = data.get("interval")
            if interval not in RECURRING_INTERVALS:
                raise ValueError(
                    f"循环间隔非法：{interval}（可选：{', '.join(RECURRING_INTERVALS)}）")
            if chime != "RANDOM":
                logger.info("循环计划提示音固定为随机，已忽略指定值")
                schedule["chime_filename"] = "RANDOM"
            schedule["interval"] = interval
            return schedule

        schedule["time"] = _parse_hhmm(data.get("time", "00:00")).strftime("%H:%M")
        if schedule_type == "weekly":
            schedule["days"] = _normalize_days(data.get("days"))
        elif schedule_type == "date":
            if data.get("date") is None:
                raise ValueError("指定日期计划需要提供 date（MM-DD）")
            month, day = _parse_month_day(data["date"])
            schedule["month"], schedule["day"] = month, day
        elif schedule_type == "holiday":
            holiday = (data.get("holiday") or "").strip()
            if holiday not in ALL_HOLIDAYS:
                raise ValueError(f"节日非法：{holiday}（可选：{'、'.join(ALL_HOLIDAYS)}）")
            schedule["holiday"] = holiday
        if not schedule["name"]:
            schedule["name"] = f"计划 {schedule.get('id', '')}".strip()
        return schedule

    def _find_conflict(self, candidate, exclude_id=None):
        """同类型同时刻的计划视为冲突（recurring 不参与时刻冲突）。"""
        ctype = candidate.get("schedule_type")
        if ctype == "recurring":
            return None
        ctime = candidate.get("time")
        for s in self.schedules:
            if not s.get("enabled", True) or s.get("id") == exclude_id:
                continue
            if s.get("schedule_type") != ctype or s.get("time") != ctime:
                continue
            if ctype == "weekly" and set(s.get("days", [])) & set(candidate.get("days", [])):
                return s
            if ctype == "date" and s.get("month") == candidate.get("month") \
                    and s.get("day") == candidate.get("day"):
                return s
            if ctype == "holiday" and s.get("holiday") == candidate.get("holiday"):
                return s
        return None

    def _has_enabled_recurring(self):
        for s in self.schedules:
            if s.get("enabled", True) and s.get("schedule_type") == "recurring":
                return True, s
        return False, None

    # -- 更新 / 删除 --------------------------------------------------------
    def update_schedule(self, schedule_id, **kwargs):
        """更新计划字段（name/type/chime/time/days/date/holiday/interval/enabled）。

        返回 (成功与否, 中文消息)。
        """
        schedule = self.get_schedule(schedule_id)
        if not schedule:
            return False, f"计划不存在：{schedule_id}"

        if kwargs.get("enabled") is True and not schedule.get("enabled", True):
            has_recurring, existing = self._has_enabled_recurring()
            ctype = kwargs.get("type", schedule.get("schedule_type"))
            if ctype == "recurring":
                if has_recurring and existing.get("id") != schedule_id:
                    return False, f"已有启用的循环计划「{existing.get('name')}」。"
                others = [s for s in self.list_schedules(enabled_only=True)
                          if s.get("schedule_type") != "recurring" and s.get("id") != schedule_id]
                if others:
                    return False, CONFIRM_DISABLE_OTHERS
            elif has_recurring:
                return False, f"循环计划「{existing.get('name')}」启用中，请先停用它。"

        new_type = kwargs.get("type", schedule.get("schedule_type"))
        if new_type not in SCHEDULE_TYPES:
            return False, f"计划类型非法：{new_type}"

        merged = dict(schedule)
        merged["schedule_type"] = new_type
        key_map = {"chime": "chime_filename"}
        for key, value in kwargs.items():
            if key in ("id", "created_at", "last_run"):
                continue
            if key == "type":
                continue
            merged[key_map.get(key, key)] = value
        # date 字段单独处理（_build_schedule 期望 data['date']）
        try:
            rebuilt = self._build_schedule(new_type, {
                "name": merged.get("name"),
                "chime": merged.get("chime_filename"),
                "time": merged.get("time", "00:00"),
                "days": merged.get("days"),
                "date": kwargs.get("date", {"month": merged.get("month"),
                                            "day": merged.get("day")}
                                   if merged.get("month") else None),
                "holiday": merged.get("holiday"),
                "interval": merged.get("interval"),
                "enabled": kwargs.get("enabled", merged.get("enabled", True)),
            }, merged.get("chime_filename") or "RANDOM")
        except ValueError as exc:
            return False, str(exc)

        conflict = self._find_conflict(rebuilt, exclude_id=schedule_id)
        if conflict:
            return False, f"与已有计划「{conflict.get('name')}」时间冲突。"

        rebuilt["id"] = schedule_id
        rebuilt["created_at"] = schedule.get("created_at")
        if schedule.get("last_run"):
            rebuilt["last_run"] = schedule["last_run"]
        if not rebuilt.get("name"):
            rebuilt["name"] = schedule.get("name") or f"计划 {schedule_id}"
        idx = self.schedules.index(schedule)
        old = self.schedules[idx]
        self.schedules[idx] = rebuilt
        if self._save():
            logger.info(f"已更新计划「{rebuilt['name']}」")
            return True, "计划更新成功"
        self.schedules[idx] = old
        return False, "保存计划失败"

    def delete_schedule(self, schedule_id):
        schedule = self.get_schedule(schedule_id)
        if not schedule:
            return False, f"计划不存在：{schedule_id}"
        self.schedules.remove(schedule)
        if self._save():
            logger.info(f"已删除计划「{schedule.get('name')}」")
            return True, "计划删除成功"
        self.schedules.append(schedule)
        return False, "保存计划失败"

    def disable_all_schedules_except(self, exclude_id=None):
        """停用除指定计划外的所有计划（UI 确认"停用其他"时用）。返回停用数量。"""
        count = 0
        for s in self.schedules:
            if s.get("id") != exclude_id and s.get("enabled", True):
                s["enabled"] = False
                count += 1
        if count:
            self._save()
        return count

    # -- 到期清理 -----------------------------------------------------------
    def cleanup_expired_date_schedules(self, check_time=None):
        """删除已执行且时刻已过的 date 类型计划。返回删除数量。"""
        now = _car_naive(check_time)
        expired = []
        for s in self.schedules:
            if s.get("schedule_type") != "date" or not s.get("last_run"):
                continue
            try:
                stime = _parse_hhmm(s.get("time", "00:00"))
                scheduled = datetime(now.year, s["month"], s["day"],
                                     stime.hour, stime.minute)
            except (ValueError, KeyError):
                continue
            if now > scheduled:
                expired.append(s["id"])
        for sid in expired:
            self.delete_schedule(sid)
        if expired:
            logger.info(f"清理了 {len(expired)} 个已过期的指定日期计划")
        return len(expired)

    # -- 判定 ---------------------------------------------------------------
    def get_active_chime(self, check_time=None):
        """判定给定时间应生效的提示音文件名；无匹配返回 None（保持当前）。

        优先级：节日计划 > 指定日期计划 > 每周计划；
        同类型取时刻最晚且已过的；今天无已过计划时回退看昨天。
        chime_filename 为 'RANDOM' 时随机挑一首（避开当前）。
        check_time 为 naive 时视为车机本地时间。
        """
        now = _car_naive(check_time)
        now_time = now.time()

        def collect(day_offset, ref):
            matched = {"holiday": [], "date": [], "weekly": []}
            ref_day = DAYS_OF_WEEK[ref.weekday()]
            ref_holidays = _holidays_for_date(ref.year, ref.month, ref.day)
            for s in self.schedules:
                if not s.get("enabled", True):
                    continue
                stype = s.get("schedule_type", "weekly")
                try:
                    stime = _parse_hhmm(s["time"])
                except (KeyError, ValueError):
                    continue
                hit = False
                if stype == "holiday" and s.get("holiday") in ref_holidays:
                    hit = True
                elif stype == "date" and s.get("month") == ref.month \
                        and s.get("day") == ref.day:
                    hit = True
                elif stype == "weekly" and ref_day in s.get("days", []):
                    hit = True
                if hit and (day_offset != 0 or now_time >= stime):
                    matched[stype].append((stime, s))
            return matched

        matched = collect(0, now)
        if not any(matched.values()):
            matched = collect(-1, now - timedelta(days=1))

        for stype in ("holiday", "date", "weekly"):
            if matched[stype]:
                stime, schedule = max(matched[stype], key=lambda item: item[0])
                chime = schedule.get("chime_filename")
                if chime == "RANDOM":
                    picked = self._select_random_chime()
                    if picked:
                        logger.info(f"{stime.strftime('%H:%M')} 随机计划选中：{picked}")
                        return picked
                    logger.warning("随机计划无可用提示音")
                    return None
                return chime
        return None

    def _select_random_chime(self, exclude_current=True):
        """从提示音曲库随机选一首；exclude_current=True 时避开当前生效的。"""
        from .lock_chime_service import list_chimes
        try:
            chimes = list_chimes()
        except OSError as exc:
            logger.error(f"读取提示音曲库失败：{exc}")
            return None
        if not chimes:
            return None
        pool = [c["name"] for c in chimes
                if not (exclude_current and c.get("is_active"))]
        if not pool:
            pool = [c["name"] for c in chimes]
        picked = random.choice(pool)
        logger.info(f"随机选中提示音：{picked}（候选 {len(pool)} 首）")
        return picked

    def should_execute_schedule(self, schedule_id, check_time=None):
        """判断某计划现在是否应执行（时刻/日期命中且今日未执行过）。

        返回 (是否执行, 提示音文件名|'RANDOM'|None, 中文原因)。
        """
        now = _car_naive(check_time)
        schedule = self.get_schedule(schedule_id)
        if not schedule:
            return False, None, f"计划不存在：{schedule_id}"
        if not schedule.get("enabled", True):
            return False, None, "计划已停用"
        if schedule.get("schedule_type") == "recurring":
            return self._should_execute_recurring(schedule, now)

        try:
            stime = _parse_hhmm(schedule["time"])
        except (KeyError, ValueError):
            return False, None, f"计划时间格式非法：{schedule.get('time')}"
        stype = schedule.get("schedule_type", "weekly")
        matched = (
            (stype == "weekly" and DAYS_OF_WEEK[now.weekday()] in schedule.get("days", []))
            or (stype == "date" and schedule.get("month") == now.month
                and schedule.get("day") == now.day)
            or (stype == "holiday" and schedule.get("holiday")
                in _holidays_for_date(now.year, now.month, now.day))
        )
        if not matched:
            return False, None, "今天不是该计划的执行日期"
        last_run = _parse_last_run(schedule.get("last_run"))
        if last_run and last_run.date() == now.date():
            return False, None, f"今日已执行过（{last_run.strftime('%H:%M')}）"
        if now.time() < stime:
            return False, None, (
                f"计划时间 {schedule['time']} 还未到（当前 {now.strftime('%H:%M')}）")
        return True, schedule.get("chime_filename"), "到达计划时间，可以执行"

    def _should_execute_recurring(self, schedule, now):
        interval = schedule.get("interval")
        if interval not in RECURRING_INTERVALS:
            return False, None, f"循环间隔非法：{interval}"
        chime = schedule.get("chime_filename", "RANDOM")
        last_run = _parse_last_run(schedule.get("last_run"))
        if last_run is None:
            return True, chime, "循环计划尚未执行过"
        if interval == "on_boot":
            boot_time = self._boot_time(now)
            if boot_time is None:
                return False, None, "无法获取系统启动时间，跳过本次"
            if last_run < boot_time:
                return True, chime, "检测到本次启动后尚未执行"
            return False, None, "本次启动已执行过"
        minutes = INTERVAL_TO_MINUTES[interval]
        elapsed = (now - last_run).total_seconds() / 60
        if elapsed >= minutes:
            return True, chime, f"距上次执行已过 {elapsed:.0f} 分钟"
        return False, None, f"距上次执行仅 {elapsed:.0f} 分钟，未到间隔"

    @staticmethod
    def _boot_time(now):
        """系统启动时间（车机本地 naive）；取不到返回 None。"""
        try:
            with open("/proc/uptime", "r") as fh:
                uptime = float(fh.read().split()[0])
            return now - timedelta(seconds=uptime)
        except (OSError, ValueError, IndexError) as exc:
            logger.warning(f"读取系统启动时间失败：{exc}")
            return None

    def record_execution(self, schedule_id, execution_time=None):
        """记录计划已执行（供 daemon/开机脚本调用）。"""
        schedule = self.get_schedule(schedule_id)
        if not schedule:
            return False
        now = _car_naive(execution_time)
        schedule["last_run"] = now.replace(tzinfo=car_tz()).isoformat()
        if self._save():
            logger.info(f"已记录计划「{schedule.get('name')}」执行时间")
            return True
        return False


# ---------------------------------------------------------------------------
# 随机分组
# ---------------------------------------------------------------------------

class ChimeGroupManager:
    """管理提示音随机分组。状态存 state/chime_groups.json。

    文件结构：{"groups": {group_id: {...}}, "random": {"enabled": bool,
             "group_id": str|None, "last_selected": str|None, "updated_at": str}}
    分组即随机池：随机模式开启后，每次 Pi 启动时从随机源分组随机挑一首设为当前。
    """

    def __init__(self, groups_file=None):
        self.groups_file = groups_file or os.path.join(get_state_dir(), "chime_groups.json")
        data = _load_json(self.groups_file, {})
        if not isinstance(data, dict):
            data = {}
        self.groups = data.get("groups", {}) if isinstance(data.get("groups"), dict) else {}
        self.random_config = data.get("random", {}) if isinstance(data.get("random"), dict) else {}
        self.random_config.setdefault("enabled", False)
        self.random_config.setdefault("group_id", None)
        self.random_config.setdefault("last_selected", None)

    # -- 持久化 ------------------------------------------------------------
    def _save(self):
        return _save_json(self.groups_file,
                          {"groups": self.groups, "random": self.random_config})

    # -- 分组 CRUD ----------------------------------------------------------
    def list_groups(self):
        """列出全部分组：[{id, name, chimes, chime_count, is_random_source}]。"""
        result = []
        for gid, gdata in self.groups.items():
            result.append({
                "id": gid,
                "name": gdata.get("name", gid),
                "chimes": list(gdata.get("chimes", [])),
                "chime_count": len(gdata.get("chimes", [])),
                "is_random_source": self.random_config.get("group_id") == gid,
                "created_at": gdata.get("created_at"),
            })
        result.sort(key=lambda g: g["name"].lower())
        return result

    def get_group(self, group_id):
        gdata = self.groups.get(group_id)
        if not gdata:
            return None
        return {
            "id": group_id,
            "name": gdata.get("name", group_id),
            "chimes": list(gdata.get("chimes", [])),
            "chime_count": len(gdata.get("chimes", [])),
            "is_random_source": self.random_config.get("group_id") == group_id,
        }

    def create_group(self, name):
        """创建分组。返回 (成功与否, 中文消息, group_id|None)。"""
        name = (name or "").strip()
        if not name:
            return False, "分组名称不能为空", None
        for gdata in self.groups.values():
            if gdata.get("name", "").lower() == name.lower():
                return False, f"已存在同名分组「{name}」", None
        gid = "".join(c if (c.isalnum() or c == "_") else "_"
                      for c in name.lower().replace(" ", "_"))
        gid = gid.strip("_") or "group"
        base, counter = gid, 2
        while gid in self.groups:
            gid = f"{base}_{counter}"
            counter += 1
        self.groups[gid] = {
            "name": name,
            "chimes": [],
            "created_at": now_car().isoformat(),
        }
        if self._save():
            logger.info(f"已创建分组「{name}」")
            return True, f"分组「{name}」创建成功", gid
        del self.groups[gid]
        return False, "保存分组失败", None

    def delete_group(self, group_id):
        """删除分组；正被用作随机源时需先关闭随机模式或更换随机源。"""
        gdata = self.groups.get(group_id)
        if not gdata:
            return False, f"分组不存在：{group_id}"
        if self.random_config.get("enabled") and self.random_config.get("group_id") == group_id:
            return False, "该分组正被用作随机源，请先关闭随机模式或更换随机源"
        del self.groups[group_id]
        if self._save():
            logger.info(f"已删除分组「{gdata.get('name')}」")
            return True, f"分组「{gdata.get('name')}」删除成功"
        self.groups[group_id] = gdata
        return False, "保存分组失败"

    def add_to_group(self, group_id, chime):
        """把提示音加入分组。返回 (成功与否, 中文消息)。"""
        gdata = self.groups.get(group_id)
        if not gdata:
            return False, f"分组不存在：{group_id}"
        chimes = gdata.setdefault("chimes", [])
        if chime in chimes:
            return False, f"「{chime}」已在该分组中"
        chimes.append(chime)
        if self._save():
            return True, f"已将「{chime}」加入分组「{gdata.get('name')}」"
        chimes.remove(chime)
        return False, "保存分组失败"

    def remove_from_group(self, group_id, chime):
        """把提示音移出分组。返回 (成功与否, 中文消息)。"""
        gdata = self.groups.get(group_id)
        if not gdata:
            return False, f"分组不存在：{group_id}"
        chimes = gdata.get("chimes", [])
        if chime not in chimes:
            return False, f"「{chime}」不在该分组中"
        chimes.remove(chime)
        if self._save():
            return True, f"已将「{chime}」移出分组「{gdata.get('name')}」"
        chimes.append(chime)
        return False, "保存分组失败"

    # -- 随机模式 -----------------------------------------------------------
    def get_random_mode(self):
        """随机模式是否开启。"""
        return bool(self.random_config.get("enabled"))

    def get_random_source(self):
        """当前随机源分组 id（未设置返回 None）。"""
        return self.random_config.get("group_id")

    def set_random_source(self, group_id):
        """设定随机源分组（随机模式需选定作用分组）。返回 (成功与否, 中文消息)。"""
        gdata = self.groups.get(group_id)
        if not gdata:
            return False, f"分组不存在：{group_id}"
        if not gdata.get("chimes"):
            return False, f"分组「{gdata.get('name')}」没有提示音，请先添加"
        self.random_config["group_id"] = group_id
        self.random_config["updated_at"] = now_car().isoformat()
        if self._save():
            logger.info(f"随机源分组已设为「{gdata.get('name')}」")
            return True, f"随机源已设为分组「{gdata.get('name')}」"
        return False, "保存随机配置失败"

    def set_random_mode(self, enabled):
        """开关随机模式。开启要求已设定非空随机源分组。

        开启后，每次 Pi 启动时从随机源分组随机挑一首设为当前
        （见 apply_boot_random_chime，供开机脚本调用）。
        返回 (成功与否, 中文消息)。
        """
        if enabled:
            gid = self.random_config.get("group_id")
            gdata = self.groups.get(gid) if gid else None
            if not gdata:
                return False, "请先设定随机源分组"
            if not gdata.get("chimes"):
                return False, f"随机源分组「{gdata.get('name')}」没有提示音，请先添加"
            self.random_config["enabled"] = True
            logger.info(f"随机模式已开启（随机源：{gdata.get('name')}）")
        else:
            self.random_config["enabled"] = False
            self.random_config["last_selected"] = None
            logger.info("随机模式已关闭")
        self.random_config["updated_at"] = now_car().isoformat()
        if self._save():
            return True, "随机模式已开启" if enabled else "随机模式已关闭"
        return False, "保存随机配置失败"

    def select_random_chime(self, avoid_chime=None):
        """从随机源分组随机选一首；avoid_chime 避开指定（通常为当前生效的）。

        未启用/无源分组/分组为空时返回 None。
        不重置随机种子，保证可测试性。
        """
        if not self.get_random_mode():
            return None
        gid = self.random_config.get("group_id")
        gdata = self.groups.get(gid) if gid else None
        if not gdata:
            logger.error(f"随机模式已开启，但随机源分组「{gid}」不存在")
            return None
        chimes = list(gdata.get("chimes", []))
        if not chimes:
            logger.error(f"随机源分组「{gdata.get('name')}」没有提示音")
            return None
        pool = [c for c in chimes if c != avoid_chime] or chimes
        picked = random.choice(pool)
        self.random_config["last_selected"] = picked
        self.random_config["updated_at"] = now_car().isoformat()
        self._save()
        logger.info(f"从分组「{gdata.get('name')}」随机选中：{picked}")
        return picked

    def apply_boot_random_chime(self):
        """开机时调用：随机模式开启时，从随机源分组随机挑一首并设为当前。

        返回 (成功与否, 中文消息|None)。
        """
        if not self.get_random_mode():
            return False, "随机模式未开启"
        from .lock_chime_service import list_chimes, set_active
        try:
            current = next((c["name"] for c in list_chimes() if c.get("is_active")), None)
        except OSError:
            current = None
        picked = self.select_random_chime(avoid_chime=current)
        if not picked:
            return False, "随机源分组中没有可用提示音"
        ok, msg = set_active(picked)
        if ok:
            logger.info(f"开机随机提示音已生效：{picked}")
        return ok, msg


def get_holidays_with_dates(year=None):
    """某年的中国节日及公历日期（供 UI 展示）。"""
    year = year or _car_naive().year
    result = []
    for name, (m, d) in sorted(FIXED_HOLIDAYS.items(), key=lambda kv: (kv[1][0], kv[1][1])):
        result.append({"name": name, "month": m, "day": d})
    for name, table in LUNAR_HOLIDAYS.items():
        md = table.get(year)
        if md:
            result.append({"name": name, "month": md[0], "day": md[1]})
    result.sort(key=lambda h: (h["month"], h["day"]))
    return result


# ---------------------------------------------------------------------------
# 模块级接口（docs/INTERFACES.md 契约）
# Workstream B 的 _services.py 按函数名查找，单例委托给上面的类。
# ---------------------------------------------------------------------------

_scheduler_instance = None
_groups_instance = None


def _sched() -> "ChimeScheduler":
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = ChimeScheduler()
    return _scheduler_instance


def _grp() -> "ChimeGroupManager":
    global _groups_instance
    if _groups_instance is None:
        _groups_instance = ChimeGroupManager()
    return _groups_instance


def list_schedules():
    """列出定时计划。"""
    return _sched().list_schedules()


def create_schedule(data):
    """新建定时计划，data={name, type, chime, days/date/holiday, enabled}。"""
    return _sched().create_schedule(data)


def update_schedule(schedule_id, **kwargs):
    """更新定时计划。"""
    return _sched().update_schedule(schedule_id, **kwargs)


def delete_schedule(schedule_id):
    """删除定时计划。"""
    return _sched().delete_schedule(schedule_id)


def list_groups():
    """列出随机分组。"""
    return _grp().list_groups()


def create_group(name):
    """新建随机分组。"""
    return _grp().create_group(name)


def add_to_group(group_id, chime):
    """向分组添加提示音。"""
    return _grp().add_to_group(group_id, chime)


def remove_from_group(group_id, chime):
    """从分组移除提示音。"""
    return _grp().remove_from_group(group_id, chime)


def delete_group(group_id):
    """删除随机分组。"""
    return _grp().delete_group(group_id)


def set_random_source(group_id):
    """设定随机模式的作用分组。"""
    return _grp().set_random_source(group_id)


def get_random_source():
    """当前随机源分组。"""
    return _grp().get_random_source()


def get_random_mode():
    """随机模式是否开启。"""
    return _grp().get_random_mode()


def set_random_mode(enabled):
    """开关随机模式。"""
    return _grp().set_random_mode(enabled)


def apply_boot_random_chime():
    """开机时从随机源分组随机设为当前（供开机脚本调用）。"""
    return _grp().apply_boot_random_chime()
