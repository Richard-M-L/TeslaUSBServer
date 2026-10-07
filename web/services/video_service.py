#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频服务（Workstream C / C2）：TeslaCam 视频发现、元数据与收藏/删除。

来源与取舍（只读移植）
----------------------
移植来源：``~/workspace/teslausb-cn-review/scripts/web/services/video_service.py``
（上游 RdeLange/TeslaUSB → fork Richard-M-L/TeslaUSB20260521 的 Flask Web 服务，
约 630 行）。

* 借鉴：摄像头文件名识别（front / left_repeater / right_repeater /
  left_pillar / right_pillar / back；精确匹配优先、子串兜底）、
  文件夹遍历（RecentClips 扁平结构 + SavedClips/SentryClips 事件子目录）、
  MP4 头校验（ftyp 魔数，过滤损坏或不可播放的文件）、
  ``estimate_recording_time`` 的"剩余空间 ÷ 已有视频平均大小"估算思路。
* 舍弃：事件/会话（event/session）分组概念、event.json / thumb.png 解析、
  加密标记（encrypted_videos）、mapping DB 耦合、present/edit 双挂载模式。
  新契约（docs/INTERFACES.md）只要扁平视频列表：同一分钟时间戳的多路
  摄像头文件归并为一条记录，``cameras`` 按实际存在的文件返回。

时区（审计 P0）
--------------
绝不使用裸 ``datetime.fromtimestamp()``。所有时间一律经
``web.services.tzutil`` 处理：优先用文件名时间（车机本地时间，
``parse_filename_time`` 返回 aware datetime），文件名解析失败才回退到
文件 mtime（``epoch_to_car``）；显示统一用 ``format_car_time``
（'%Y-%m-%d %H:%M'）。这直接修复审计定位的 mapping_service.py:504 类
bug（mvhd UTC 时间戳与文件名北京时间混用，导致恒差 8 小时）。

路径约定
--------
挂载根与状态目录来自 ``web.services.config``：
``get_teslacam_root()`` → ``<mount>/part1/TeslaCam``，
``get_state_dir()`` → 收藏数据 ``favorites.json`` 的存放目录。

其它约定
--------
* ``thumbnail_url`` 只给出 URL 规则 ``/thumbs/<folder>/<name>.jpg``，
  实际缩略图生成由 Workstream B / 后续阶段实现，服务层不生成文件。
* ``duration_s``：有 ffprobe 时探测单路文件的真实时长并缓存；无 ffprobe
  或解析失败时按特斯拉 1 分钟一片的惯例回退为 60s（日志中标注）。
* 用户可见的日志一律中文；仅用标准库。
"""

import json
import logging
import os
import re
import shutil
import subprocess

from web.services.tzutil import parse_filename_time, format_car_time, epoch_to_car
from web.services.config import get_teslacam_root, get_state_dir

logger = logging.getLogger(__name__)

# 特斯拉真实文件夹结构
FOLDERS = ("RecentClips", "SavedClips", "SentryClips")

# 摄像头标识（规范顺序，与播放页 2x3 方位布局对应）
CAMERAS = ("front", "left_repeater", "right_repeater",
           "left_pillar", "right_pillar", "back")

# 文件名格式：2026-10-06_14-32-11-front.mp4
_FILENAME_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(?P<cam>.+)\.mp4$",
    re.IGNORECASE,
)

# MP4 ftyp 魔数（移植自 fork 的 is_valid_mp4）
_MP4_FTYP = b"ftyp"

# 特斯拉每段行车记录约为 1 分钟：ffprobe 不可用/解析失败时的回退值
_FALLBACK_DURATION_S = 60

# 无历史视频时的理论码率：约 400MB/小时（移植自 fork 的 estimate_recording_time）
_THEORETICAL_BYTES_PER_HOUR = 400 * 1024 * 1024

_FFPROBE = shutil.which("ffprobe")
_duration_cache = {}


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _check_folder(folder):
    """校验 folder 参数，非法时抛 ValueError。"""
    if folder not in FOLDERS:
        raise ValueError("未知视频文件夹：%r，应为 %s 之一" % (folder, "/".join(FOLDERS)))


def _resolve_folders(folder):
    """folder=None 表示全部三个文件夹；否则校验后返回单元素元组。"""
    if folder is None:
        return FOLDERS
    _check_folder(folder)
    return (folder,)


def is_valid_mp4(filepath):
    """检查文件头是否含 ftyp（移植自 fork，用于过滤损坏/不可播文件）。"""
    try:
        with open(filepath, "rb") as f:
            header = f.read(12)
        return len(header) >= 12 and _MP4_FTYP in header
    except (OSError, IOError):
        return False


def _match_camera(token):
    """从文件名摄像头段识别摄像头标识；无法识别返回 None。

    精确匹配优先（大小写不敏感），再用子串兜底兼容非标准命名，
    兜底时长名称优先以避免误判。
    """
    t = token.lower()
    if t in CAMERAS:
        return t
    for cam in ("left_repeater", "right_repeater", "left_pillar",
                "right_pillar", "front", "back"):
        if cam in t:
            return cam
    return None


def _search_dirs(base):
    """待扫描目录：文件夹本身（RecentClips 扁平结构）+ 下一级事件子目录。

    SavedClips/SentryClips 的事件子目录只深入一层，与特斯拉实际结构一致。
    """
    dirs = [base]
    try:
        with os.scandir(base) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    dirs.append(entry.path)
    except OSError:
        pass
    return dirs


def _collect_groups(folder):
    """扫描单个文件夹，按"分钟时间戳"把多路摄像头文件归并为组。

    返回 {session: {"files": {camera: {"path", "size", "mtime"}}}}。
    无效 MP4（头校验失败）直接跳过并记中文日志。
    """
    groups = {}
    base = os.path.join(get_teslacam_root(), folder)
    for dirpath in _search_dirs(base):
        try:
            with os.scandir(dirpath) as entries:
                for entry in entries:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if not entry.name.lower().endswith(".mp4"):
                        continue
                    m = _FILENAME_RE.match(entry.name)
                    if not m:
                        continue
                    camera = _match_camera(m.group("cam"))
                    if camera is None:
                        continue
                    if not is_valid_mp4(entry.path):
                        logger.info("跳过无效的 MP4 文件（头校验失败）：%s", entry.path)
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    group = groups.setdefault(m.group("ts"), {"files": {}})
                    # 同一摄像头只保留第一个文件
                    if camera not in group["files"]:
                        group["files"][camera] = {
                            "path": entry.path,
                            "size": st.st_size,
                            "mtime": st.st_mtime,
                        }
        except OSError as exc:
            logger.warning("无法扫描视频目录 %s：%s", dirpath, exc)
    return groups


def _car_time_for(filename, mtime):
    """取一条记录的车机本地时间（aware datetime）。

    优先用文件名时间（parse_filename_time）；解析失败（返回 None 或抛异常）
    时回退到文件 mtime（epoch_to_car）。绝不使用裸 fromtimestamp。
    """
    try:
        dt = parse_filename_time(filename)
    except Exception as exc:  # 兼容解析失败抛异常的实现
        logger.debug("文件名时间解析失败 %s：%s，回退到文件 mtime", filename, exc)
        dt = None
    if dt is None:
        dt = epoch_to_car(mtime)
    return dt


def _duration_s(path):
    """取视频时长（秒，整数）。

    有 ffprobe 时探测该组第一路文件的真实时长并按路径缓存；
    无 ffprobe 或解析失败时回退为 60s（特斯拉 1 分钟一片的惯例），
    回退情况记 debug 日志标注。
    """
    cached = _duration_cache.get(path)
    if cached is not None:
        return cached
    duration = _FALLBACK_DURATION_S
    if _FFPROBE:
        try:
            proc = subprocess.run(
                [_FFPROBE, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=15,
            )
            value = float(proc.stdout.strip().split()[0])
            if value > 0:
                duration = int(round(value))
        except Exception as exc:
            logger.debug("ffprobe 解析时长失败 %s：%s，按 60s 回退", path, exc)
    else:
        logger.debug("未找到 ffprobe，视频时长按 60s 回退：%s", path)
    _duration_cache[path] = duration
    return duration


def _fav_key(folder, name):
    return "%s/%s" % (folder, name)


def _favorites_path():
    state_dir = get_state_dir()
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "favorites.json")


def _load_favorites():
    """读收藏集合；文件缺失或损坏时返回空集合。"""
    try:
        with open(_favorites_path(), encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("favorites", []))
    except (OSError, ValueError) as exc:
        logger.debug("读取收藏文件失败（将视为空）：%s", exc)
        return set()


def _save_favorites(favorites):
    """原子写收藏集合（先写临时文件再 rename）。"""
    path = _favorites_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"favorites": sorted(favorites)}, f,
                  ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 公开接口（docs/INTERFACES.md → videos）
# ---------------------------------------------------------------------------

def list_videos(folder=None, favorite_only=False):
    """列出视频（扁平列表，按时间倒序）。

    每条记录为同一分钟时间戳的多路摄像头文件归并：
    {name, folder, time(车机本地时间 '%Y-%m-%d %H:%M'), duration_s,
     cameras(按实际存在的文件，规范顺序), size_mb, thumbnail_url, favorite}。

    :param folder: RecentClips/SavedClips/SentryClips 之一，None 表示全部。
    :param favorite_only: 仅返回已收藏。
    """
    folders = _resolve_folders(folder)
    favorites = _load_favorites()
    items = []
    for f in folders:
        for session, group in _collect_groups(f).items():
            files = group["files"]
            first = files.get("front") or next(iter(files.values()))
            dt = _car_time_for(os.path.basename(first["path"]), first["mtime"])
            total_size = sum(info["size"] for info in files.values())
            items.append({
                "name": session,
                "folder": f,
                "time": format_car_time(dt),
                "duration_s": _duration_s(first["path"]),
                "cameras": [c for c in CAMERAS if c in files],
                "size_mb": round(total_size / (1024 * 1024), 2),
                "thumbnail_url": "/thumbs/%s/%s.jpg" % (f, session),
                "favorite": _fav_key(f, session) in favorites,
                "_sort_ts": dt.timestamp(),
            })
    if favorite_only:
        items = [it for it in items if it["favorite"]]
    items.sort(key=lambda it: it["_sort_ts"], reverse=True)
    for it in items:
        del it["_sort_ts"]
    return items


def get_folder_stats():
    """各文件夹统计：{RecentClips: {count, size_gb}, ...}。

    count 为视频条数（与 list_videos 条目对应，即分钟级记录数），
    size_gb 为有效 MP4 文件大小合计。文件夹不存在时计 0。
    """
    stats = {}
    for folder in FOLDERS:
        groups = _collect_groups(folder)
        total = sum(info["size"]
                    for g in groups.values() for info in g["files"].values())
        stats[folder] = {
            "count": len(groups),
            "size_gb": round(total / (1024 ** 3), 2),
        }
    return stats


def estimate_recording_time():
    """估算剩余可录制时长：{hours, method, confidence}。

    思路移植自 fork 的 estimate_recording_time：用分区剩余空间 ÷ 已有视频
    平均大小估算；无历史视频时用 400MB/小时理论值。
    与 fork 不同：以"分钟级记录（多路摄像头合计）"为单位，而非单个文件，
    避免把 6 路摄像头重复计为 6 分钟。
    """
    try:
        free_bytes = shutil.disk_usage(get_teslacam_root()).free
    except OSError as exc:
        logger.warning("无法获取 TeslaCam 分区剩余空间：%s", exc)
        return {"hours": None, "method": "存储不可用", "confidence": "low"}

    total_bytes = 0
    n_records = 0
    for folder in FOLDERS:
        for group in _collect_groups(folder).values():
            n_records += 1
            total_bytes += sum(info["size"] for info in group["files"].values())

    if n_records == 0:
        hours = free_bytes / _THEORETICAL_BYTES_PER_HOUR
        return {
            "hours": round(hours, 1),
            "method": "理论值（约 400MB/小时，尚无历史视频）",
            "confidence": "low",
        }

    avg_bytes_per_minute = total_bytes / n_records
    hours = free_bytes / avg_bytes_per_minute / 60 if avg_bytes_per_minute > 0 else 0
    return {
        "hours": round(hours, 1),
        "method": "按现有 %d 条视频的平均大小估算" % n_records,
        "confidence": "high" if n_records > 100 else ("medium" if n_records > 10 else "low"),
    }


def toggle_favorite(name, folder):
    """切换收藏状态，返回切换后的状态（True=已收藏）。

    收藏以 "folder/name" 存于 state 目录的 favorites.json。
    """
    _check_folder(folder)
    favorites = _load_favorites()
    key = _fav_key(folder, name)
    if key in favorites:
        favorites.remove(key)
        new_state = False
        logger.info("已取消收藏：%s", key)
    else:
        favorites.add(key)
        new_state = True
        logger.info("已收藏：%s", key)
    _save_favorites(favorites)
    return new_state


def delete_videos(names, folder):
    """删除指定视频（该分钟时间戳的全部摄像头文件）。

    同时清理其收藏标记；删除后若事件子目录变空则一并移除
    （含 event.json 等非视频文件的目录会被保留）。
    返回 {"deleted_names", "deleted_files", "failed": {name: 原因}}，
    原因一律中文。调用方需先做二次确认（见 INTERFACES.md）。
    """
    _check_folder(folder)
    base = os.path.join(get_teslacam_root(), folder)
    deleted_names = []
    deleted_files = 0
    failed = {}
    favorites = _load_favorites()
    fav_changed = False

    for name in names:
        targets = []
        scan_error = None
        for dirpath in _search_dirs(base):
            try:
                with os.scandir(dirpath) as entries:
                    for entry in entries:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if not entry.name.lower().endswith(".mp4"):
                            continue
                        m = _FILENAME_RE.match(entry.name)
                        if m and m.group("ts") == name:
                            targets.append(entry.path)
            except OSError as exc:
                scan_error = "无法扫描目录：%s" % exc
                break
        if scan_error:
            failed[name] = scan_error
            continue
        if not targets:
            failed[name] = "未找到该视频"
            continue
        ok = True
        for path in targets:
            try:
                os.remove(path)
                deleted_files += 1
            except OSError as exc:
                ok = False
                failed[name] = "删除文件失败：%s" % exc
                break
        if ok:
            deleted_names.append(name)
            key = _fav_key(folder, name)
            if key in favorites:
                favorites.remove(key)
                fav_changed = True

    if fav_changed:
        _save_favorites(favorites)

    # 清理变空的事件子目录
    try:
        with os.scandir(base) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    try:
                        os.rmdir(entry.path)
                        logger.info("已清理空目录：%s", entry.path)
                    except OSError:
                        pass  # 非空则保留
    except OSError:
        pass

    logger.info("删除视频：成功 %d 条（%d 个文件），失败 %d 条",
                len(deleted_names), deleted_files, len(failed))
    return {
        "deleted_names": deleted_names,
        "deleted_files": deleted_files,
        "failed": failed,
    }
