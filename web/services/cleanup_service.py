"""自动清理服务：按文件夹保留天数删除过期视频。

规则（``config.yaml`` 的 ``cleanup`` 节，可被 ``state/cleanup_retention.json``
覆盖——并行创建的 ``web/services/config.py`` 只提供读取接口，因此用户在
Web 界面的修改持久化到 state/，优先级高于 config.yaml）：

- ``recentclips_retention_days`` / ``sentryclips_retention_days``：
  视频早于 N 天则删除；
- ``savedclips_retention_days = 0`` 表示不清理（手动保存的视频默认保留，
  任何文件夹的 0 都表示"不自动清理"）；
- ``protect_last_hour = true``：1 小时内的视频永不删除（刚停车就触发
  清理时不能误删正在写入/刚写完的片段）；
- ``run_on_boot``：开机自动清理（由 ``boot_cleanup_if_enabled`` 执行）。

年龄比较直接用 epoch 时间戳，不涉及任何时区转换（时区 P0 只在
``format_car_time`` 这类展示环节出现）。

用户可见的日志与进度消息一律中文。
"""

import json
import logging
import os

from web.services.config import (
    get_teslacam_root, get_state_dir, get_config_value,
)
from web.services.jobs import (
    start_job, get_job_progress as _jobs_progress,
    cancel_job as _jobs_cancel,
)

logger = logging.getLogger(__name__)

CLEANUP_FOLDERS = ("RecentClips", "SavedClips", "SentryClips")
VIDEO_EXTS = (".mp4", ".MP4")

# 文件夹 → config.yaml 中的保留天数键
RETENTION_KEYS = {
    "RecentClips": "recentclips_retention_days",
    "SavedClips": "savedclips_retention_days",
    "SentryClips": "sentryclips_retention_days",
}

DEFAULTS = {
    "run_on_boot": False,
    "recentclips_retention_days": 7,
    "savedclips_retention_days": 0,
    "sentryclips_retention_days": 30,
    "protect_last_hour": True,
}

_RETENTION_OVERRIDE_FILE = "cleanup_retention.json"
_PROTECT_SECONDS = 3600  # 1 小时保护

# job 进度里的文件计数（与 backup_service 相同的无竞态模式：
# worker 持 counters 闭包，run_cleanup 返回后再建 job_id 索引）。
_job_counters = {}


def _override_path():
    d = get_state_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, _RETENTION_OVERRIDE_FILE)


def _effective_config():
    """defaults ← config.yaml ← state/cleanup_retention.json。"""
    cfg = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        cfg[key] = get_config_value("cleanup", key, default=default)
    path = _override_path()
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                override = json.load(f)
            if isinstance(override, dict):
                for key in DEFAULTS:
                    if key in override:
                        cfg[key] = override[key]
        except (ValueError, OSError) as e:
            logger.error("读取清理配置覆盖文件失败：%s", e)
    return cfg


def get_retention_config():
    """返回保留策略配置 dict。"""
    return _effective_config()


def set_retention_config(cfg):
    """保存保留策略（校验后写入 state/cleanup_retention.json）。

    Raises:
        ValueError: 未知键或非法值（中文信息）。
    """
    cfg = dict(cfg or {})
    unknown = [k for k in cfg if k not in DEFAULTS]
    if unknown:
        raise ValueError("未知的清理配置项：%s。" % "、".join(unknown))
    clean = {}
    for key, value in cfg.items():
        if key.endswith("_retention_days"):
            try:
                days = int(value)
            except (TypeError, ValueError):
                raise ValueError("保留天数必须是非负整数：%s。" % key)
            if days < 0:
                raise ValueError("保留天数不能为负数：%s。" % key)
            clean[key] = days
        else:
            clean[key] = bool(value)
    path = _override_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    logger.info("清理保留策略已更新：%s", clean)
    return True


def _iter_video_files():
    """ yields (folder, path, size, mtime)。"""
    root = get_teslacam_root()
    for folder in CLEANUP_FOLDERS:
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
                st = os.stat(p)
            except OSError:
                continue
            yield folder, p, st.st_size, st.st_mtime


def _collect_deletable(now_ts=None):
    """按规则收集可删除文件，返回 [(path, size)]。

    - retention_days <= 0 的文件夹跳过（0 = 不自动清理）；
    - protect_last_hour 为 true 时，1 小时内的文件永不删除；
    - 只有早于 retention_days 的文件才入选。
    """
    import time as _time
    now_ts = now_ts if now_ts is not None else _time.time()
    cfg = _effective_config()
    protect = bool(cfg.get("protect_last_hour", True))
    deletable = []
    for folder, path, size, mtime in _iter_video_files():
        days = cfg.get(RETENTION_KEYS[folder], 0)
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 0
        if days <= 0:
            continue  # 0 表示不自动清理
        age = now_ts - mtime
        if protect and age < _PROTECT_SECONDS:
            continue  # 1 小时内的视频永不删除
        if age > days * 86400:
            deletable.append((path, size))
    return deletable


def preview_cleanup():
    """预览：返回 {files, size_gb}，不删除任何文件。"""
    deletable = _collect_deletable()
    total = sum(s for _, s in deletable)
    return {"files": len(deletable), "size_gb": round(total / (1024 ** 3), 2)}


def _cleanup_worker(progress, counters):
    deletable = _collect_deletable()
    total = len(deletable)
    counters.update(current=0, total=total)
    if total == 0:
        progress(100, "没有符合清理条件的文件。")
        logger.info("清理完成：没有符合条件的文件。")
        return

    root = os.path.realpath(get_teslacam_root())
    deleted, freed = 0, 0
    # 取消是协作式的：jobs.py 约定 cancel 后下一次 progress 调用即抛
    # 内部异常；循环每次结尾都调 progress，因此无需额外查询
    for i, (path, size) in enumerate(deletable):
        real = os.path.realpath(path)
        if os.path.commonpath([root, real]) != root:
            logger.error("拒绝删除 TeslaCam 之外的文件：%s", path)
            continue
        try:
            os.remove(real)
            deleted += 1
            freed += size
        except OSError as e:
            logger.error("删除文件失败 %s：%s", path, e)
        counters.update(current=i + 1, total=total)
        progress(int((i + 1) / total * 100),
                 "正在清理 %d/%d…" % (i + 1, total))

    progress(100, "清理完成，共删除 %d 个文件。" % deleted)
    logger.info("清理完成：删除 %d 个文件，释放 %.2f GB。",
                deleted, freed / (1024 ** 3))


def run_cleanup():
    """启动清理任务，返回 job_id。"""
    counters = {"current": 0, "total": 0}

    def _target(progress):
        _cleanup_worker(progress, counters)

    job_id = start_job("cleanup", _target)
    _job_counters[job_id] = counters
    return job_id


def get_cleanup_progress(job_id):
    """返回 {state, percent, message, current, total}。"""
    base = _jobs_progress(job_id)
    counters = _job_counters.get(job_id, {"current": 0, "total": 0})
    return {"state": base.get("state", "unknown"),
            "percent": base.get("percent", 0),
            "message": base.get("message", ""),
            "current": counters.get("current", 0),
            "total": counters.get("total", 0)}


def get_job_progress(job_id):
    """通用进度查询（委托 jobs）。"""
    return _jobs_progress(job_id)


def cancel_job(job_id):
    """取消清理任务。"""
    return _jobs_cancel(job_id)


def boot_cleanup_if_enabled():
    """开机调用：run_on_boot 为 true 时启动清理，返回 job_id 或 None。"""
    if get_config_value("cleanup", "run_on_boot", default=False):
        logger.info("开机自动清理已启用，启动清理任务。")
        return run_cleanup()
    return None
