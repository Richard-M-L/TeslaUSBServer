"""归档生产者：周期性扫描 TeslaCam → SEI peek（静止判断）→ 入队。

单守护线程，每 60s 走一遍 TeslaCam 只读挂载的
``RecentClips/``、``SentryClips/``（事件子目录）、``SavedClips/``
（事件子目录），把新 ``.mp4`` 送入 ``archive_queue``。幂等：
队列的 UNIQUE 约束让重复扫描很便宜。

职责：
  1. 开机补扫——服务停机期间 Tesla 写的新片段；
  2. 覆盖 inotify 遗漏（内核缓冲溢出、挂载抖动等）；
  3. VFS 缓存漂移时的兜底重扫。

SEI peek（静止片段源头过滤）：RecentClips 候选在入队前先 peek；
SEI 里没有任何移动信号（``SeiMessage.has_movement``，**不依赖 GPS**）
的片段直接跳过、不进队列。跳过计数记在内存 deque 里供设置页徽标展示。

抛弃的来源 legacy 代码：
  * ``_peek_clip_for_gps`` → ``_peek_clip_for_movement``（及注释措辞），
    来源的 GPS 命名全部消除；
  * VFS 缓存刷新 ``_refresh_ro_mount`` 调用：依赖的 mapping_service
    在本版本未移植，已移除；
  * 来源 peek 失败（None）时「放行入队、交给 worker 处理」的 fail-open
    策略：本版本改为 fail-closed——SEI 解析失败则**跳过并记中文日志**，
    不崩溃、不入队（写稳定门已保证只 peek 足够旧的文件）。

公开 API：
  * ``start_producer(teslacam_root, db_path, *, rescan_interval_seconds,
    boot_catchup_enabled, boot_scan_defer_seconds)``
  * ``stop_producer(timeout)``
  * ``get_producer_status()``
  * ``run_boot_catchup_once(teslacam_root, db_path)``
  * ``enqueue_with_peek(paths, db_path=None)``
  * ``get_skipped_stationary_count(hours)`` /
    ``reset_skipped_stationary_tally()``
  * ``reset_peek_cache()`` / ``get_peek_cache_stats()``
"""

from __future__ import annotations

import collections
import importlib
import logging
import os
import threading
import time
from typing import Dict, Iterable, List, Optional

from web.services.archive_worker import (
    PRIORITY_RECENT_CLIPS,
    _SKIP_MOVEMENT_PEEK_MAX_MESSAGES,
    _SKIP_MOVEMENT_PEEK_MAX_WALK_BYTES,
    _SKIP_MOVEMENT_PEEK_SAMPLE_RATE,
    enqueue_many_for_archive,
    infer_priority,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 可调参数（模块级，测试可 monkeypatch）
# ---------------------------------------------------------------------------

# 每次扫描走的子目录；顺序即优先级（RecentClips 最先）。
_WATCH_SUBDIRS = ('RecentClips', 'SentryClips', 'SavedClips')

_DEFAULT_RESCAN_INTERVAL = 60.0

# 对 RecentClips 候选做 SEI peek 前要求的最小文件年龄（秒）：
# 太新的文件 Tesla 可能还在写，此时 peek 会误判为静止。
_STABLE_WRITE_AGE_FALLBACK = 5.0


def _stable_write_age_seconds() -> float:
    """返回写稳定等待秒数（与 worker 取同一配置）。"""
    try:
        from web.services.archive_worker import _stable_write_age_seconds \
            as _worker_age
        return float(_worker_age())
    except Exception:  # noqa: BLE001
        return _STABLE_WRITE_AGE_FALLBACK


# ---------------------------------------------------------------------------
# 模块状态（经 _state_lock 访问）
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_state: Dict = {
    'running': False,
    'teslacam_root': None,
    'db_path': None,
    'rescan_interval_seconds': _DEFAULT_RESCAN_INTERVAL,
    'boot_catchup_enabled': True,
    'iterations': 0,
    'last_scan_at': None,
    'last_enqueued': 0,
    'last_seen': 0,
    'last_skipped_stationary': 0,
    'last_error': None,
    'started_at': None,
}

# 源头跳过的滚动计数（内存 deque，24h 徽标用；重启清零可接受，
# 避免为计数器多写 SD）。
_SKIPPED_TALLY_MAX = 10000
_skipped_tally_lock = threading.Lock()
_skipped_tally: 'collections.deque[float]' = collections.deque(
    maxlen=_SKIPPED_TALLY_MAX,
)


# ---------------------------------------------------------------------------
# SEI-peek 决策缓存（Issue #208 思路的移植）
# ---------------------------------------------------------------------------
# 「这个 RecentClips 路径已判定静止」的缓存，key=path，
# value=(mtime, size, cached_at_monotonic)。命中即跳过 peek，
# 不再 mmap 文件。Tesla 原地覆盖轮转片段时 mtime+size 必变，
# 失配即失效重 peek。

_PEEK_CACHE_MAX_ENTRIES = 1000
_PEEK_CACHE_TTL_SECONDS = 24 * 3600
_peek_cache_lock = threading.Lock()
_peek_cache: Dict[str, tuple] = {}
_peek_cache_stats: Dict[str, int] = {
    'hits': 0,
    'misses': 0,
    'invalidations': 0,
    'evictions': 0,
}


def _is_running() -> bool:
    with _state_lock:
        t = _thread
    return t is not None and t.is_alive()


def reset_skipped_stationary_tally() -> None:
    """清空源头跳过计数（测试/管理用）。"""
    with _skipped_tally_lock:
        _skipped_tally.clear()


def get_skipped_stationary_count(hours: int = 24) -> int:
    """返回最近 N 小时内在生产者源头跳过的静止片段数。"""
    if hours <= 0:
        return 0
    horizon = time.time() - (int(hours) * 3600)
    with _skipped_tally_lock:
        while _skipped_tally and _skipped_tally[0] < horizon:
            _skipped_tally.popleft()
        return len(_skipped_tally)


def _record_skip() -> None:
    with _skipped_tally_lock:
        _skipped_tally.append(time.time())


def _peek_cache_lookup(path: str, mtime: float, size: int) -> bool:
    """path 在该 (mtime, size) 下是否已缓存为静止。"""
    now = time.monotonic()
    with _peek_cache_lock:
        entry = _peek_cache.get(path)
        if entry is None:
            _peek_cache_stats['misses'] += 1
            return False
        cached_mtime, cached_size, cached_at = entry
        if now - cached_at > _PEEK_CACHE_TTL_SECONDS:
            del _peek_cache[path]
            _peek_cache_stats['evictions'] += 1
            _peek_cache_stats['misses'] += 1
            return False
        if cached_mtime == mtime and cached_size == size:
            _peek_cache_stats['hits'] += 1
            return True
        del _peek_cache[path]
        _peek_cache_stats['invalidations'] += 1
        _peek_cache_stats['misses'] += 1
        return False


def _peek_cache_store(path: str, mtime: float, size: int) -> None:
    """记录「path 在此 (mtime, size) 下 peek 为静止」。"""
    now = time.monotonic()
    with _peek_cache_lock:
        if (path not in _peek_cache
                and len(_peek_cache) >= _PEEK_CACHE_MAX_ENTRIES):
            victims = sorted(
                _peek_cache.items(), key=lambda kv: kv[1][2],
            )[: max(1, _PEEK_CACHE_MAX_ENTRIES // 4)]
            for victim_path, _ in victims:
                del _peek_cache[victim_path]
                _peek_cache_stats['evictions'] += 1
        _peek_cache[path] = (mtime, size, now)


def reset_peek_cache() -> None:
    """清空 SEI-peek 决策缓存（测试/管理用）。"""
    with _peek_cache_lock:
        _peek_cache.clear()


def get_peek_cache_stats() -> Dict[str, int]:
    """返回缓存大小与累计命中/未命中计数（供系统健康页）。"""
    with _peek_cache_lock:
        snapshot = dict(_peek_cache_stats)
        snapshot['size'] = len(_peek_cache)
        snapshot['capacity'] = _PEEK_CACHE_MAX_ENTRIES
    return snapshot


def _peek_clip_for_movement(source_path: str) -> Optional[bool]:
    """生产者侧的 SEI 快速 peek。

    与 ``archive_worker._clip_has_movement`` 同样的三态语义：
    True=有移动信号（值得归档），False=静止（跳过），
    None=无法判断（解析失败等）。
    判断只看 ``SeiMessage.has_movement``（速度/档位/Autopilot），
    **不依赖 GPS**——国行车机 SEI 无 GPS 坐标，本函数依然有效。
    """
    try:
        sei_parser = importlib.import_module('web.services.sei_parser')
    except Exception as e:  # noqa: BLE001
        logger.debug("SEI 解析模块不可用（%s），peek 推迟", e)
        return None

    scanned = 0
    try:
        for msg in sei_parser.extract_sei_messages(
                source_path,
                sample_rate=_SKIP_MOVEMENT_PEEK_SAMPLE_RATE,
                max_walk_bytes=_SKIP_MOVEMENT_PEEK_MAX_WALK_BYTES):
            scanned += 1
            if msg.has_movement:
                return True
            if scanned >= _SKIP_MOVEMENT_PEEK_MAX_MESSAGES:
                break
        return False
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("SEI peek 失败：%s（%s）", source_path, e)
        return None


def enqueue_with_peek(paths: Iterable[str],
                      db_path: Optional[str] = None) -> Dict[str, int]:
    """带 SEI peek 的批量入队。

    * 非 RecentClips（哨兵/已保存事件片段）：直接入队，不 peek。
    * RecentClips：文件太新（Tesla 可能还在写）→ 直接入队，交给
      worker 的写稳定门；足够旧 → SEI peek：
      - False（静止）→ 记跳过计数，**不入队**；
      - None（解析失败）→ **跳过并记中文日志**，不入队、不崩溃；
      - True（有移动信号）→ 正常入队。

    返回 ``{enqueued, skipped_stationary, considered}``。
    """
    pending_enqueue: List[str] = []
    skipped = 0
    considered = 0
    stable_age = _stable_write_age_seconds()
    for raw in paths:
        if not raw:
            continue
        considered += 1
        if infer_priority(raw) != PRIORITY_RECENT_CLIPS:
            pending_enqueue.append(raw)
            continue
        # RecentClips：先过新鲜度门，再 peek。
        try:
            st = os.stat(raw)
        except OSError:
            # watcher 触发到我们 stat 之间文件消失了：丢弃。
            continue
        mtime, size = st.st_mtime, st.st_size
        if (time.time() - mtime) < stable_age:
            # 太新：不做静止判断，交给 worker 的写稳定门。
            pending_enqueue.append(raw)
            continue
        if _peek_cache_lookup(raw, mtime, size):
            _record_skip()
            skipped += 1
            logger.debug("跳过静止片段（缓存命中）：%s", raw)
            continue
        verdict = _peek_clip_for_movement(raw)
        if verdict is False:
            _record_skip()
            skipped += 1
            _peek_cache_store(raw, mtime, size)
            logger.debug("跳过静止片段（SEI 无移动信号）：%s", raw)
            continue
        if verdict is None:
            # 解析失败：跳过并记日志（不入队，避免坏文件污染队列）。
            logger.warning("SEI 解析失败，已跳过该片段：%s", raw)
            continue
        pending_enqueue.append(raw)

    enqueued = 0
    if pending_enqueue:
        try:
            enqueued = enqueue_many_for_archive(
                pending_enqueue, db_path=db_path,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("批量入队失败：%s", e)
    return {
        'enqueued': enqueued,
        'skipped_stationary': skipped,
        'considered': considered,
    }


# ---------------------------------------------------------------------------
# 目录扫描
# ---------------------------------------------------------------------------

def _iter_archive_candidates(teslacam_root: str) -> List[str]:
    """返回被监视子目录下的全部 ``.mp4``。

    RecentClips 走一层（平铺文件），SentryClips/SavedClips 走两层
    （事件子目录）。子目录缺失或无权限时静默跳过——下次扫描补上。
    返回绝对路径，顺序稳定。
    """
    out: List[str] = []
    if not teslacam_root or not os.path.isdir(teslacam_root):
        return out
    for sub in _WATCH_SUBDIRS:
        sub_path = os.path.join(teslacam_root, sub)
        if not os.path.isdir(sub_path):
            continue
        try:
            entries = list(os.scandir(sub_path))
        except (PermissionError, OSError):
            continue
        for entry in entries:
            try:
                if entry.is_file(follow_symlinks=False):
                    if entry.name.lower().endswith('.mp4'):
                        out.append(entry.path)
                elif entry.is_dir(follow_symlinks=False):
                    try:
                        for clip in os.scandir(entry.path):
                            if (clip.is_file(follow_symlinks=False)
                                    and clip.name.lower().endswith('.mp4')):
                                out.append(clip.path)
                    except (PermissionError, OSError):
                        continue
            except OSError:
                continue
    return out


def _scan_once(teslacam_root: str, db_path: str) -> Dict[str, int]:
    """执行一次扫描。返回 ``{seen, enqueued, skipped_stationary}``。"""
    seen = _iter_archive_candidates(teslacam_root)
    if not seen:
        return {'seen': 0, 'enqueued': 0, 'skipped_stationary': 0}
    result = enqueue_with_peek(seen, db_path=db_path)
    enqueued = int(result.get('enqueued', 0))
    skipped = int(result.get('skipped_stationary', 0))
    if enqueued > 0 or skipped > 0:
        logger.info(
            "本次扫描：入队 %d 个，跳过静止 %d 个（共发现 %d 个）",
            enqueued, skipped, len(seen),
        )
    return {
        'seen': len(seen),
        'enqueued': enqueued,
        'skipped_stationary': skipped,
    }


def run_boot_catchup_once(teslacam_root: str,
                          db_path: Optional[str] = None) -> Dict[str, int]:
    """同步执行一次补扫（供测试/直接调用；勿在请求线程调用）。"""
    return _scan_once(teslacam_root, db_path or '')


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------

def start_producer(teslacam_root: str,
                   db_path: Optional[str] = None,
                   *,
                   rescan_interval_seconds: float = _DEFAULT_RESCAN_INTERVAL,
                   boot_catchup_enabled: bool = True,
                   boot_scan_defer_seconds: float = 0.0) -> bool:
    """启动生产者线程。幂等：已在运行返回 False。

    ``boot_scan_defer_seconds`` > 0 时，首次补扫延迟执行，避免与开机
    初始化风暴叠加（Zero 2W 上曾因此触发看门狗重启）。
    """
    global _thread
    with _state_lock:
        if _thread is not None and _thread.is_alive():
            logger.debug("归档生产者已在运行")
            return False
        _stop_event.clear()
        _state['running'] = True
        _state['teslacam_root'] = teslacam_root
        _state['db_path'] = db_path
        _state['rescan_interval_seconds'] = float(rescan_interval_seconds)
        _state['boot_catchup_enabled'] = bool(boot_catchup_enabled)
        _state['boot_scan_defer_seconds'] = float(boot_scan_defer_seconds)
        _state['iterations'] = 0
        _state['last_scan_at'] = None
        _state['last_enqueued'] = 0
        _state['last_seen'] = 0
        _state['last_error'] = None
        _state['started_at'] = time.time()
        _thread = threading.Thread(
            target=_run_loop,
            args=(teslacam_root, db_path,
                  float(rescan_interval_seconds),
                  bool(boot_catchup_enabled),
                  float(boot_scan_defer_seconds)),
            name='archive-producer',
            daemon=True,
        )
        _thread.start()
    logger.info(
        "归档生产者已启动（目录=%s，间隔=%.1fs，开机补扫=%s，首次延迟=%.1fs）",
        teslacam_root, rescan_interval_seconds,
        boot_catchup_enabled, boot_scan_defer_seconds,
    )
    return True


def stop_producer(timeout: float = 10.0) -> bool:
    """停止生产者并等待退出。超时返回 False。"""
    global _thread
    with _state_lock:
        thread = _thread
    if thread is None:
        return True
    _stop_event.set()
    thread.join(timeout=timeout)
    exited = not thread.is_alive()
    if exited:
        with _state_lock:
            if _thread is thread:
                _thread = None
            _state['running'] = False
        logger.info("归档生产者已干净停止")
    else:
        logger.warning("归档生产者在 %.1fs 内未退出", timeout)
    return exited


def get_producer_status() -> Dict:
    """生产者状态快照（供观测接口）。"""
    with _state_lock:
        snap = dict(_state)
    snap['running'] = _is_running()
    snap['skipped_stationary_24h'] = get_skipped_stationary_count(24)
    return snap


def _run_loop(teslacam_root: str, db_path: Optional[str],
              rescan_interval_seconds: float,
              boot_catchup_enabled: bool,
              boot_scan_defer_seconds: float = 0.0) -> None:
    """生产者线程主体：任何单次扫描异常都不能杀死线程。"""
    if not boot_catchup_enabled:
        if _stop_event.wait(rescan_interval_seconds):
            with _state_lock:
                _state['running'] = False
            return
    elif boot_scan_defer_seconds > 0:
        if _stop_event.wait(boot_scan_defer_seconds):
            with _state_lock:
                _state['running'] = False
            return

    while not _stop_event.is_set():
        try:
            result = _scan_once(teslacam_root, db_path or '')
            with _state_lock:
                _state['iterations'] += 1
                _state['last_scan_at'] = time.time()
                _state['last_seen'] = int(result.get('seen', 0))
                _state['last_enqueued'] = int(result.get('enqueued', 0))
                _state['last_skipped_stationary'] = int(
                    result.get('skipped_stationary', 0))
                _state['last_error'] = None
        except Exception as e:  # noqa: BLE001 — 生产者永不死
            logger.exception("归档生产者某次扫描失败")
            with _state_lock:
                _state['last_error'] = str(e)

        if _stop_event.wait(rescan_interval_seconds):
            break

    with _state_lock:
        _state['running'] = False
