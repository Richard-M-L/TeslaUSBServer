"""NAS 备份服务：扫描 TeslaCam 三个文件夹 → rclone sync → 写历史。

流程：
1. ``start_backup()`` 启动后台 job，扫描
   ``RecentClips / SavedClips / SentryClips`` 下的视频；
2. 逐文件夹 ``rclone sync`` 到 NAS（断点续传见 rclone_service）；
3. 成功后写 ``state/backup_history.json``；
4. 若 ``config.yaml`` 的 ``backup.delete_after_backup`` 为 true，
   删除本地已备份文件——删除前**二次读取配置确认**，防止用户在
   备份过程中关闭了开关。

rclone 缺失 / 未配置 NAS 时：job 直接进入 failed 并给出中文提示，
不向调用方抛异常（start_backup 恒返回 job_id）。

用户可见的日志与进度消息一律中文。
"""

import json
import logging
import os

from web.services import rclone_service
from web.services.config import (
    get_teslacam_root, get_state_dir, get_config_value,
)
from web.services.jobs import (
    start_job, get_job_progress as _jobs_progress, cancel_job as _jobs_cancel,
)
try:
    # 真 config.py 带缓存；二次校验前清缓存，保证读到最新磁盘值
    from web.services.config import reload_config as _reload_config
except ImportError:  # 单测替身无此函数
    _reload_config = None
from web.services.tzutil import format_car_time, now_car

logger = logging.getLogger(__name__)

# 特斯拉行车记录仪的三个真实文件夹
BACKUP_FOLDERS = ("RecentClips", "SavedClips", "SentryClips")
VIDEO_EXTS = (".mp4", ".MP4")

_HISTORY_FILE = "backup_history.json"
_HISTORY_KEEP = 30

# job 进度里的文件计数。target 签名只有 progress、拿不到 job_id，
# 且线程可能在 start_job 返回前启动（holder 竞态），因此 worker 直接
# 持有 counters 闭包；start_backup 返回后再建 job_id -> counters 索引。
_job_counters = {}


def _history_path():
    d = get_state_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, _HISTORY_FILE)


def _scan_videos():
    """扫描三个文件夹，返回 [(folder, path, size)]。"""
    root = get_teslacam_root()
    found = []
    for folder in BACKUP_FOLDERS:
        d = os.path.join(root, folder)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(VIDEO_EXTS):
                continue
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            try:
                found.append((folder, p, os.path.getsize(p)))
            except OSError:
                continue
    return found


def _append_history(files, size_bytes, status):
    entry = {
        "time": format_car_time(now_car()),
        "files": files,
        "size_gb": round(size_bytes / (1024 ** 3), 2),
        "status": status,
    }
    path = _history_path()
    history = []
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                history = json.load(f)
        except (ValueError, OSError):
            history = []
    history.insert(0, entry)
    history = history[:_HISTORY_KEEP]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get_backup_history():
    """返回 [{time, files, size_gb, status}]（新在前）。"""
    path = _history_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (ValueError, OSError):
        return []


def _is_cancel_exc(exc):
    """是否为 jobs 协作式取消抛出的内部异常。

    jobs.py 约定：cancel 后任务下一次调用 progress 即抛内部异常
    （实现中名为 _JobCancelled）；此处按名称识别，避免导入私有符号。
    """
    return type(exc).__name__ == "_JobCancelled"


def _sync_one_folder(local_dir, remote_subpath, progress_cb,
                     total_bytes, base_bytes):
    """单文件夹同步（测试缝：单测可 monkeypatch 此函数伪造 rclone）。"""
    return rclone_service.sync_folder(
        local_dir, remote_subpath, progress_cb=progress_cb,
        total_bytes=total_bytes, base_bytes=base_bytes)


def _backup_worker(progress, counters):
    """后台备份主流程。失败通过抛异常让 jobs 记 failed（中文信息）。"""
    # --- 预检 ---
    if not rclone_service.rclone_available():
        raise RuntimeError("未找到 rclone，无法执行备份，请先安装 rclone。")
    if not rclone_service.get_nas_config().get("configured"):
        raise RuntimeError("尚未配置 NAS，请先在设置页填写 NAS 连接信息。")
    videos = _scan_videos()
    if not videos:
        progress(100, "TeslaCam 中没有视频文件，无需备份。")
        logger.info("备份跳过：没有可备份的视频文件。")
        return

    total_files = len(videos)
    total_bytes = sum(s for _, _, s in videos)
    counters.update(current=0, total=total_files)
    logger.info("开始备份：%d 个文件，共 %.2f GB。",
                total_files, total_bytes / (1024 ** 3))

    root = get_teslacam_root()
    done_files = 0
    done_bytes = 0
    try:
        for folder in BACKUP_FOLDERS:
            items = [(p, s) for f, p, s in videos if f == folder]
            if not items:
                continue
            local_dir = os.path.join(root, folder)
            folder_bytes = sum(s for _, s in items)

            def _cb(pct, msg, _done=done_files, _total=total_files):
                progress(pct, "正在备份 %d/%d…" % (_done, _total))

            _sync_one_folder(local_dir, "TeslaCam/%s" % folder,
                             progress_cb=_cb,
                             total_bytes=total_bytes,
                             base_bytes=done_bytes)
            done_files += len(items)
            done_bytes += folder_bytes
            counters.update(current=done_files, total=total_files)
            pct = int(done_bytes / total_bytes * 100) if total_bytes else 100
            progress(min(99, pct), "正在备份 %d/%d…" % (done_files, total_files))
    except Exception as e:
        if _is_cancel_exc(e):
            # 协作式取消：记一条"已取消"历史后继续抛出，由 jobs 记
            # state=cancelled（此处不可再调 progress，会再次抛异常）
            logger.info("备份已取消：已完成 %d/%d 个文件。", done_files, total_files)
            _append_history(done_files, done_bytes, "已取消")
        raise

    progress(100, "备份完成。")
    logger.info("备份完成：%d 个文件，共 %.2f GB。",
                total_files, total_bytes / (1024 ** 3))
    _append_history(total_files, total_bytes, "成功")

    # --- 备份成功后删除本地：二次校验配置 ---
    _maybe_delete_after_backup(videos, progress)


def _maybe_delete_after_backup(videos, progress):
    """delete_after_backup 二次校验 + 删除本地文件。"""
    if not get_config_value("backup", "delete_after_backup", default=False):
        return
    # 二次校验：删除是危险操作，清配置缓存后重新读取，防止备份过程中
    # 用户在 Web 界面关闭了开关（TOCTOU 防护）。
    if _reload_config is not None:
        _reload_config()
    if not get_config_value("backup", "delete_after_backup", default=False):
        logger.info("delete_after_backup 在备份过程中被关闭，跳过删除本地文件。")
        return
    logger.warning("备份成功且 delete_after_backup=true（已二次确认），"
                   "正在删除本地 %d 个已备份视频文件。", len(videos))
    deleted, failed = 0, 0
    root = os.path.realpath(get_teslacam_root())
    for folder, path, _size in videos:
        real = os.path.realpath(path)
        if os.path.commonpath([root, real]) != root:
            logger.error("拒绝删除 TeslaCam 之外的文件：%s", path)
            failed += 1
            continue
        try:
            os.remove(real)
            deleted += 1
        except OSError as e:
            logger.error("删除本地文件失败 %s：%s", path, e)
            failed += 1
    logger.warning("本地文件删除完毕：成功 %d 个，失败 %d 个。", deleted, failed)
    progress(100, "备份完成，已删除本地 %d 个文件。" % deleted)


def start_backup():
    """启动备份，返回 job_id（预检失败时 job 直接记 failed，不抛异常）。"""
    counters = {"current": 0, "total": 0}

    def _target(progress):
        _backup_worker(progress, counters)

    job_id = start_job("backup", _target)
    _job_counters[job_id] = counters
    return job_id


def get_backup_progress(job_id):
    """返回 {state, percent, message, current, total}。"""
    base = _jobs_progress(job_id)
    counters = _job_counters.get(job_id, {"current": 0, "total": 0})
    out = {"state": base.get("state", "unknown"),
           "percent": base.get("percent", 0),
           "message": base.get("message", ""),
           "current": counters.get("current", 0),
           "total": counters.get("total", 0)}
    return out


def get_job_progress(job_id):
    """通用进度查询（委托 jobs）。"""
    return _jobs_progress(job_id)


def cancel_job(job_id):
    """取消备份任务。"""
    return _jobs_cancel(job_id)
