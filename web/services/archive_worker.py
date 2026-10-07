"""归档工作线程：取队 → 原子拷贝 → 校验 → 写 SEI sidecar。

单线程从 ``archive_queue`` 取出待归档行，把 TeslaCam 只读挂载上的
``.mp4`` 原子拷贝到归档目录（``~/ArchivedClips`` 风格的 ``archive_root``，
保持 ``TeslaCam/<子目录>/`` 相对布局），拷贝成功后写 SEI sidecar JSON
并把目标路径送入索引队列。

抛弃的来源 legacy 代码（~/workspace/teslausb-cn-review 对应实现）：
  * ``pipeline_queue`` 影子双写与校验：``_dual_write_pipeline_archive*``
    系列、``_shadow_pipeline_queue_enabled`` / ``_shadow_compare_picks`` /
    ``_shadow_agreement_count`` 等影子对比逻辑，以及
    ``_use_pipeline_reader_enabled`` / ``_claim_via_pipeline_reader``
    的 flag 门控分支 —— 全部删除，只保留 ``archive_queue`` 单路径。
  * ``task_coordinator`` 锁竞争：本模块不再依赖任务协调器；
    与索引线程的公平性改由各自的低优先级 + pause/resume 协作保证。
  * 磁盘满时的自动清理触发 ``_maybe_trigger_critical_cleanup``（依赖
    清理工作线的 ``archive_watchdog``，不属于本工作线）：磁盘守卫只做
    「暂停认领 + 释放回 pending」，清理逻辑由 cleanup 服务负责。
  * ``_adaptive_load_threshold`` / ``_adaptive_chunk_pause`` /
    ``_disk_fullness_pct`` 的磁盘占用自适应层：保留 ``_atomic_copy``
    自身的负载感知 chunk 暂停与单文件时间预算（这是拷贝核心的一部分），
    删掉按磁盘占用率动态调参的外层。
  * drain-rate / ETA 统计（``_compute_drain_rate`` / ``compute_eta_seconds``）
    与 legacy 树内 ``.partial`` 兜底清扫：观测性附加功能，本版本不移植；
    孤儿清扫只扫 ``.staging`` 目录。
  * GPS 命名的全部标识：``_clip_has_gps_signal`` → ``_clip_has_movement``，
    ``_SKIP_GPS_PEEK_*`` → ``_SKIP_MOVEMENT_PEEK_*``。移动判断只用
    ``SeiMessage.has_movement``（速度/档位/Autopilot），不依赖 GPS。

队列实现说明：来源把队列放在独立的 ``archive_queue.py`` 模块；
本任务只允许创建四个文件，因此把收敛后的最小队列实现内嵌在本模块
（SQLite，默认 ``state/archive_queue.db``）。对外 API 命名与来源
``archive_queue`` 保持一致（``enqueue_many_for_archive`` /
``claim_next_for_worker`` / ``mark_*``），producer 直接从这里导入。

时区约定：绝不裸用 ``datetime.fromtimestamp()``。入库时间统一用
``web.services.tzutil``（``now_car`` / ``format_car_time``）；若该模块
尚未就绪则回退到 UTC ISO（绝不依赖系统本地时区）。

硬约束（继承自来源，不可破坏）：
  * 本模块绝不触碰 USB gadget（不 mount/umount/losetup）。
  * 暂停/停止时绝不在持有任何锁的情况下 sleep。
  * 日志一律中文。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 路径与配置
# ---------------------------------------------------------------------------

def _repo_root() -> str:
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _state_dir() -> str:
    """返回状态目录。优先用 web.services.config.get_state_dir()。"""
    try:
        mod = importlib.import_module('web.services.config')
        getter = getattr(mod, 'get_state_dir', None)
        if getter is not None:
            d = getter()
            if d:
                return str(d)
    except Exception:  # noqa: BLE001 — 并行创建中或不可用时回退
        pass
    return os.path.join(_repo_root(), 'state')


def _default_db_path() -> str:
    return os.path.join(_state_dir(), 'archive_queue.db')


def _cfg_get(dotted_path: str, default: Any) -> Any:
    """从配置读取点分路径，读不到返回 default。

    先试 ``web.services.config.get``（并行创建），再试 ``web.config.get``。
    """
    for modname in ('web.services.config', 'web.config'):
        try:
            mod = importlib.import_module(modname)
            getter = getattr(mod, 'get', None)
            if getter is None:
                continue
            v = getter(dotted_path, None)
            if v is not None:
                return v
        except Exception:  # noqa: BLE001
            continue
    return default


def _now_iso() -> str:
    """当前车机时间的 ISO 字符串（入库用）。"""
    try:
        tzutil = importlib.import_module('web.services.tzutil')
        return tzutil.format_car_time(tzutil.now_car())
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 可调参数（模块级，测试可 monkeypatch）
# ---------------------------------------------------------------------------

_INTER_FILE_SLEEP_SECONDS = 1.0
_IDLE_SLEEP_SECONDS = 5.0
_BACKOFF_SLEEP_SECONDS = 0.5
_DEFAULT_STOP_TIMEOUT = 15.0
_DEFAULT_PAUSE_TIMEOUT = 15.0

# 写稳定等待：文件修改后多久才认为 Tesla 写完。
_STABLE_WRITE_AGE_SECONDS = 5.0
# RecentClips 按 ~60s 分段写、moov 在段尾追加，需要更长的静置窗口。
_RECENT_CLIPS_STABLE_WRITE_AGE_SECONDS = 90.0

_DEFAULT_COPY_CHUNK_BYTES = 1024 * 1024
_RETRY_MAX_ATTEMPTS = 3
_LOAD_PAUSE_THRESHOLD = 3.5
_LOAD_PAUSE_SECONDS = 30.0
_CHUNK_PAUSE_SECONDS = 0.25
_PER_FILE_TIME_BUDGET_SECONDS = 60.0
_MOOV_VERIFY_MAX_HEADER_READS = 512
# moov 缺失（Tesla 还在写）的最大顺延次数，超过则按失败处理。
_MOOV_DEFER_CAP = 10

# 磁盘守卫阈值（MB）。
_DISK_WARN_MB = 1024
_DISK_CRITICAL_MB = 512
_DISK_PAUSE_SECONDS = 300.0

# 陈旧认领回收：认领超过这么久仍未完成，视为 worker 已死。
_STALE_CLAIM_MAX_AGE_SECONDS = 1800.0

# 静止判断 SEI 快速 peek 的调参（与 producer 共享）。
_SKIP_MOVEMENT_PEEK_MAX_MESSAGES = 90
_SKIP_MOVEMENT_PEEK_SAMPLE_RATE = 30
_SKIP_MOVEMENT_PEEK_MAX_WALK_BYTES = 2 * 1024 * 1024
# peek 解析失败且文件 mtime 比该阈值更旧：视为永久不可读，按静止处理。
_PEEK_GIVE_UP_AGE_SECONDS = 300.0


# ---------------------------------------------------------------------------
# 归档队列（SQLite，内嵌最小实现）
# ---------------------------------------------------------------------------

PRIORITY_EVENTS = 1        # SentryClips / SavedClips：事件片段最优先
PRIORITY_RECENT_CLIPS = 2  # RecentClips：行车记录，静止片段会被 SEI peek 跳过
PRIORITY_OTHER = 3         # 其他

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS archive_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path     TEXT NOT NULL UNIQUE,
    priority        INTEGER NOT NULL DEFAULT 3,
    status          TEXT NOT NULL DEFAULT 'pending',
    enqueued_at     TEXT NOT NULL,
    expected_size   INTEGER,
    expected_mtime  REAL,
    claimed_by      TEXT,
    claimed_at      REAL,
    dest_path       TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    copied_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_archive_queue_ready
    ON archive_queue(status, priority, expected_mtime, id);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    db_path = _resolve_db_path(db_path)
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(_SCHEMA_SQL)
    return conn


def _resolve_db_path(db_path: Optional[str]) -> str:
    if db_path:
        return db_path
    return _default_db_path()


def infer_priority(path: str) -> int:
    """按 TeslaCam 文件夹推断归档优先级（数字越小越先处理）。"""
    norm = (path or '').replace('\\', '/').lower()
    if '/sentryclips/' in norm or '/savedclips/' in norm:
        return PRIORITY_EVENTS
    if '/recentclips/' in norm:
        return PRIORITY_RECENT_CLIPS
    return PRIORITY_OTHER


def enqueue_for_archive(source_path: str, *,
                        priority: Optional[int] = None,
                        db_path: Optional[str] = None) -> bool:
    """幂等入队单个源文件。已存在（任意状态）则返回 False。"""
    return enqueue_many_for_archive([source_path], priority=priority,
                                    db_path=db_path) == 1


def enqueue_many_for_archive(source_paths, *,
                             priority: Optional[int] = None,
                             db_path: Optional[str] = None) -> int:
    """批量幂等入队。返回新插入的行数。"""
    paths = [p for p in source_paths if p]
    if not paths:
        return 0
    enqueued_at = _now_iso()
    rows = []
    for p in paths:
        prio = priority if priority is not None else infer_priority(p)
        try:
            st = os.stat(p)
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = None, None
        rows.append((p, int(prio), enqueued_at, size, mtime))
    conn = None
    try:
        conn = _connect(db_path or '')
        before = conn.total_changes
        conn.execute('BEGIN IMMEDIATE')
        try:
            conn.executemany(
                """
                INSERT OR IGNORE INTO archive_queue
                    (source_path, priority, status,
                     enqueued_at, expected_size, expected_mtime)
                VALUES (?, ?, 'pending', ?, ?, ?)
                """,
                rows,
            )
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            raise
        return max(0, conn.total_changes - before)
    except sqlite3.Error as e:
        logger.warning("归档入队失败：%s", e)
        return 0
    finally:
        if conn is not None:
            conn.close()


def claim_next_for_worker(claimed_by: str,
                          db_path: Optional[str] = None) -> Optional[Dict]:
    """原子认领下一个 pending 行（优先级→mtime→id 排序）。

    返回普通 dict，无待处理行返回 None。
    """
    conn = None
    try:
        conn = _connect(db_path or '')
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute(
                """
                SELECT id FROM archive_queue
                 WHERE status = 'pending'
                 ORDER BY priority ASC,
                          expected_mtime IS NULL,
                          expected_mtime ASC,
                          id ASC
                 LIMIT 1
                """,
            ).fetchone()
            if row is None:
                conn.execute('COMMIT')
                return None
            claimed_at = time.time()
            cur = conn.execute(
                """
                UPDATE archive_queue
                   SET status = 'claimed',
                       claimed_by = ?,
                       claimed_at = ?
                 WHERE id = ? AND status = 'pending'
                """,
                (claimed_by, claimed_at, int(row['id'])),
            )
            if cur.rowcount != 1:
                conn.execute('ROLLBACK')
                return None
            full = conn.execute(
                "SELECT * FROM archive_queue WHERE id = ?",
                (int(row['id']),),
            ).fetchone()
            conn.execute('COMMIT')
            result = dict(full)
            result['claimed_by'] = claimed_by
            result['claimed_at'] = claimed_at
            return result
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
    except sqlite3.Error as e:
        logger.warning("认领归档任务失败：%s", e)
        return None
    finally:
        if conn is not None:
            conn.close()


def release_claim(row_id: int, *,
                  expected_size: Optional[int] = None,
                  expected_mtime: Optional[float] = None,
                  db_path: Optional[str] = None) -> bool:
    """把认领释放回 pending（不消耗重试次数），可顺带刷新元数据。"""
    if not row_id:
        return False
    conn = None
    try:
        conn = _connect(db_path or '')
        if expected_size is not None or expected_mtime is not None:
            cur = conn.execute(
                """
                UPDATE archive_queue
                   SET status = 'pending',
                       claimed_by = NULL,
                       claimed_at = NULL,
                       expected_size = COALESCE(?, expected_size),
                       expected_mtime = COALESCE(?, expected_mtime)
                 WHERE id = ?
                """,
                (expected_size, expected_mtime, int(row_id)),
            )
        else:
            cur = conn.execute(
                """
                UPDATE archive_queue
                   SET status = 'pending',
                       claimed_by = NULL,
                       claimed_at = NULL
                 WHERE id = ?
                """,
                (int(row_id),),
            )
        conn.commit()
        return cur.rowcount == 1
    except sqlite3.Error as e:
        logger.warning("释放认领失败 id=%s：%s", row_id, e)
        return False
    finally:
        if conn is not None:
            conn.close()


def mark_copied(row_id: int, dest_path: str,
                db_path: Optional[str] = None) -> bool:
    """标记拷贝成功。"""
    if not row_id:
        return False
    conn = None
    try:
        conn = _connect(db_path or '')
        cur = conn.execute(
            """
            UPDATE archive_queue
               SET status = 'copied',
                   dest_path = ?,
                   copied_at = ?,
                   claimed_by = NULL,
                   claimed_at = NULL,
                   last_error = NULL
             WHERE id = ?
            """,
            (dest_path, _now_iso(), int(row_id)),
        )
        conn.commit()
        return cur.rowcount == 1
    except sqlite3.Error as e:
        logger.warning("标记已拷贝失败 id=%s：%s", row_id, e)
        return False
    finally:
        if conn is not None:
            conn.close()


def mark_source_gone(row_id: int,
                     db_path: Optional[str] = None) -> bool:
    """源文件已消失（Tesla 轮转掉了），终态，不再重试。"""
    if not row_id:
        return False
    conn = None
    try:
        conn = _connect(db_path or '')
        cur = conn.execute(
            """
            UPDATE archive_queue
               SET status = 'source_gone',
                   claimed_by = NULL,
                   claimed_at = NULL
             WHERE id = ?
            """,
            (int(row_id),),
        )
        conn.commit()
        return cur.rowcount == 1
    except sqlite3.Error as e:
        logger.warning("标记源文件消失失败 id=%s：%s", row_id, e)
        return False
    finally:
        if conn is not None:
            conn.close()


def mark_skipped_stationary(row_id: int,
                            db_path: Optional[str] = None) -> bool:
    """SEI peek 判定为静止的 RecentClips，终态跳过。"""
    if not row_id:
        return False
    conn = None
    try:
        conn = _connect(db_path or '')
        cur = conn.execute(
            """
            UPDATE archive_queue
               SET status = 'skipped_stationary',
                   claimed_by = NULL,
                   claimed_at = NULL
             WHERE id = ?
            """,
            (int(row_id),),
        )
        conn.commit()
        return cur.rowcount == 1
    except sqlite3.Error as e:
        logger.warning("标记静止跳过失败 id=%s：%s", row_id, e)
        return False
    finally:
        if conn is not None:
            conn.close()


def mark_failed(row_id: int, error: str, *,
                max_attempts: int = _RETRY_MAX_ATTEMPTS,
                db_path: Optional[str] = None) -> str:
    """记录一次失败；次数用尽则进 dead_letter。

    返回新状态：``'pending'``（还有重试机会）/ ``'dead_letter'`` /
    ``'error'``（DB 异常，行未动）。
    """
    if not row_id:
        return 'error'
    truncated = (error or '')[:4096]
    conn = None
    try:
        conn = _connect(db_path or '')
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute(
                "SELECT attempts FROM archive_queue WHERE id = ?",
                (int(row_id),),
            ).fetchone()
            if row is None:
                conn.execute('ROLLBACK')
                return 'error'
            new_attempts = int(row['attempts'] or 0) + 1
            new_status = (
                'dead_letter' if new_attempts >= int(max_attempts)
                else 'pending'
            )
            conn.execute(
                """
                UPDATE archive_queue
                   SET status = ?,
                       attempts = ?,
                       last_error = ?,
                       claimed_by = NULL,
                       claimed_at = NULL
                 WHERE id = ?
                """,
                (new_status, new_attempts, truncated, int(row_id)),
            )
            conn.execute('COMMIT')
            return new_status
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
    except sqlite3.Error as e:
        logger.warning("标记失败失败 id=%s：%s", row_id, e)
        return 'error'
    finally:
        if conn is not None:
            conn.close()


def recover_stale_claims(db_path: Optional[str] = None,
                         max_age_seconds: float = _STALE_CLAIM_MAX_AGE_SECONDS) -> int:
    """把认领超时（worker 疑似死亡）的行释放回 pending。返回释放数。"""
    conn = None
    try:
        conn = _connect(db_path or '')
        cutoff = time.time() - float(max_age_seconds)
        cur = conn.execute(
            """
            UPDATE archive_queue
               SET status = 'pending',
                   claimed_by = NULL,
                   claimed_at = NULL
             WHERE status = 'claimed' AND claimed_at < ?
            """,
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount
    except sqlite3.Error as e:
        logger.warning("回收陈旧认领失败：%s", e)
        return 0
    finally:
        if conn is not None:
            conn.close()


def get_queue_status(db_path: Optional[str] = None) -> Dict[str, int]:
    """按状态统计队列行数。"""
    conn = None
    try:
        conn = _connect(db_path or '')
        out: Dict[str, int] = {}
        for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM archive_queue GROUP BY status"):
            out[str(r['status'])] = int(r['n'])
        return out
    except sqlite3.Error as e:
        logger.warning("读取队列状态失败：%s", e)
        return {}
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# 目标路径与暂存区
# ---------------------------------------------------------------------------

def compute_dest_path(source_path: str, archive_root: str,
                      teslacam_root: Optional[str]) -> str:
    """把 ``source_path`` 映射到归档目录下的目标路径。

    保持 ``TeslaCam/<子目录>/...`` 相对布局；源不在 ``teslacam_root``
    下时回退到 ``archive_root/<文件名>``。
    """
    if not source_path:
        raise ValueError("source_path required")
    archive_root = os.path.abspath(archive_root)
    src_abs = os.path.abspath(source_path)
    if teslacam_root:
        tc_abs = os.path.abspath(teslacam_root).rstrip(os.sep) + os.sep
        if src_abs.startswith(tc_abs):
            return os.path.join(archive_root, src_abs[len(tc_abs):])
    return os.path.join(archive_root, os.path.basename(src_abs))


def _safe_stat(path: str):
    try:
        return os.stat(path)
    except OSError:
        return None


_STAGING_DIRNAME = '.staging'


def _staging_root(archive_root: str) -> str:
    return os.path.join(archive_root, _STAGING_DIRNAME)


def _sweep_partial_orphans(archive_root: str) -> int:
    """删除 ``.staging`` 下残留的 ``*.partial``（上次崩溃遗留）。"""
    removed = 0
    staging = _staging_root(archive_root)
    try:
        entries = list(os.scandir(staging))
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_file(follow_symlinks=False) and \
                    entry.name.endswith('.partial'):
                os.remove(entry.path)
                removed += 1
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------------------
# 原子拷贝
# ---------------------------------------------------------------------------

class _CopyTimeBudgetExceeded(OSError):
    """单文件拷贝超过时间预算（系统过载信号，非文件损坏）。"""


class _CopyMoovIncomplete(OSError):
    """拷贝出的 MP4 缺 moov/mdat（Tesla 很可能还在写）。"""


_moov_defer_counts: Dict[str, int] = {}
_moov_defer_lock = threading.Lock()


def _bump_moov_defer_count(source_path: str) -> int:
    with _moov_defer_lock:
        n = _moov_defer_counts.get(source_path, 0) + 1
        _moov_defer_counts[source_path] = n
        return n


def _reset_moov_defer_count(source_path: str) -> None:
    with _moov_defer_lock:
        _moov_defer_counts.pop(source_path, None)


def _verify_destination_complete(dest_path: str) -> bool:
    """校验 MP4 同时含有 ftyp、moov、mdat 三个顶层 box。

    流式只读 box 头（不加载整个文件）。Tesla 在片段关闭时才追加
    moov，因此「大小一致但缺 moov」意味着拷贝的是半成品。
    """
    try:
        file_size = os.path.getsize(dest_path)
        if file_size < 16:
            return False
        with open(dest_path, 'rb') as f:
            head = f.read(12)
            if len(head) < 12 or head[4:8] != b'ftyp':
                return False
            pos, reads = 0, 0
            seen_moov = seen_mdat = False
            while pos + 8 <= file_size:
                if reads >= _MOOV_VERIFY_MAX_HEADER_READS:
                    return False
                reads += 1
                f.seek(pos)
                header = f.read(8)
                if len(header) < 8:
                    return False
                size = int.from_bytes(header[:4], 'big')
                box_type = header[4:8]
                if size == 1:
                    if pos + 16 > file_size:
                        return False
                    ext = f.read(8)
                    if len(ext) < 8:
                        return False
                    size = int.from_bytes(ext, 'big')
                    if size < 16:
                        return False
                elif size == 0:
                    if box_type == b'moov':
                        seen_moov = True
                    elif box_type == b'mdat':
                        seen_mdat = True
                    return seen_moov and seen_mdat
                elif size < 8:
                    return False
                if pos + size > file_size:
                    return False
                if box_type == b'moov':
                    seen_moov = True
                elif box_type == b'mdat':
                    seen_mdat = True
                if seen_moov and seen_mdat:
                    return True
                pos += size
            return seen_moov and seen_mdat
    except (OSError, IOError):
        return False


def _atomic_copy(source_path: str, dest_path: str,
                 chunk_size: int, *,
                 load_pause_threshold: float = 0.0,
                 chunk_pause_seconds: float = 0.25,
                 chunk_pause_always: bool = False,
                 time_budget_seconds: float = 0.0,
                 staging_root: Optional[str] = None,
                 now_fn: Callable[[], float] = time.monotonic,
                 sleep_fn: Callable[[float], None] = time.sleep) -> int:
    """原子拷贝 ``source_path`` → ``dest_path``，返回字节数。

    先写 ``<archive_root>/.staging/<hash>-<name>.partial``，fsync 后
    ``os.replace`` 到目标。拷贝中按 chunk 感知负载暂停（保护 Zero 2W
    的 SDIO 总线与看门狗），超时间预算抛 ``_CopyTimeBudgetExceeded``，
    目标缺 moov/mdat 抛 ``_CopyMoovIncomplete``。失败时清理 partial。
    """
    parent = os.path.dirname(dest_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if staging_root:
        os.makedirs(staging_root, exist_ok=True)
        digest = hashlib.sha1(
            os.path.abspath(dest_path).encode('utf-8', errors='replace'),
        ).hexdigest()[:10]
        partial = os.path.join(
            staging_root,
            f"{digest}-{os.path.basename(dest_path)}.partial",
        )
    else:
        partial = dest_path + '.partial'
    expected = os.path.getsize(source_path)
    written = 0
    started = now_fn()
    deadline = started + time_budget_seconds if time_budget_seconds > 0 else 0.0
    try:
        with open(source_path, 'rb') as src, open(partial, 'wb') as dst:
            while True:
                chunk = src.read(chunk_size)
                if not chunk:
                    break
                dst.write(chunk)
                written += len(chunk)
                if deadline > 0.0 and now_fn() >= deadline:
                    raise _CopyTimeBudgetExceeded(
                        f"拷贝超过 {time_budget_seconds:.1f}s 预算 "
                        f"（已写 {written}/{expected} 字节）"
                    )
                if chunk_pause_always:
                    if chunk_pause_seconds > 0:
                        sleep_fn(chunk_pause_seconds)
                elif load_pause_threshold > 0:
                    try:
                        load1 = os.getloadavg()[0]
                    except (AttributeError, OSError):
                        load1 = 0.0
                    if load1 > load_pause_threshold:
                        sleep_fn(chunk_pause_seconds)
            dst.flush()
            try:
                os.fsync(dst.fileno())
            except OSError:
                pass  # tmpfs 等不支持 fsync 的文件系统，尽力而为
        if written != expected:
            raise OSError(f"大小不一致：写了 {written}，期望 {expected}")
        if dest_path.lower().endswith('.mp4'):
            if not _verify_destination_complete(partial):
                raise _CopyMoovIncomplete(
                    f"目标 MP4 缺 moov/mdat box，源可能还在写入：{source_path}"
                )
        try:
            shutil.copystat(source_path, partial)
        except OSError:
            pass
        os.replace(partial, dest_path)
        return written
    except Exception:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# 磁盘守卫（简化版：只暂停，不触发清理——清理由 cleanup 服务负责）
# ---------------------------------------------------------------------------

_disk_pause_lock = threading.Lock()
_disk_space_pause_until = 0.0


def _disk_thresholds_mb() -> tuple:
    warn = _cfg_get('archive.disk_warn_mb', _DISK_WARN_MB)
    crit = _cfg_get('archive.disk_critical_mb', _DISK_CRITICAL_MB)
    try:
        return int(warn), int(crit)
    except (TypeError, ValueError):
        return _DISK_WARN_MB, _DISK_CRITICAL_MB


def _check_disk_space_guard(archive_root: str) -> str:
    """检查归档目录剩余空间：``'ok'`` / ``'warning'`` / ``'critical'``。

    critical 表示禁止新的拷贝。stat 失败返回 'ok'（瞬时抖动不应锁死归档）。
    """
    try:
        usage = shutil.disk_usage(archive_root)
    except OSError:
        return 'ok'
    free_mb = int(usage.free // (1024 * 1024))
    warn_mb, crit_mb = _disk_thresholds_mb()
    if free_mb < crit_mb:
        logger.critical(
            "归档磁盘空间严重不足：%s 仅剩 %d MB（阈值 %d MB），暂停新拷贝",
            archive_root, free_mb, crit_mb,
        )
        return 'critical'
    if free_mb < warn_mb:
        logger.warning(
            "归档磁盘空间偏低：%s 仅剩 %d MB（阈值 %d MB），继续拷贝",
            archive_root, free_mb, warn_mb,
        )
        return 'warning'
    return 'ok'


# ---------------------------------------------------------------------------
# SEI peek：静止判断（不依赖 GPS）
# ---------------------------------------------------------------------------

def _stable_write_age_seconds() -> float:
    try:
        return float(_cfg_get(
            'archive.stable_write_age_seconds', _STABLE_WRITE_AGE_SECONDS))
    except (TypeError, ValueError):
        return _STABLE_WRITE_AGE_SECONDS


def _recent_clips_stable_write_age_seconds() -> float:
    try:
        return float(_cfg_get(
            'archive.recent_clips_stable_write_age_seconds',
            _RECENT_CLIPS_STABLE_WRITE_AGE_SECONDS))
    except (TypeError, ValueError):
        return _RECENT_CLIPS_STABLE_WRITE_AGE_SECONDS


def _peek_give_up_age_seconds() -> float:
    try:
        return float(_cfg_get(
            'archive.peek_give_up_age_seconds', _PEEK_GIVE_UP_AGE_SECONDS))
    except (TypeError, ValueError):
        return _PEEK_GIVE_UP_AGE_SECONDS


def _is_recent_clips_priority(row: Dict[str, Any]) -> bool:
    try:
        return int(row.get('priority', 0)) == PRIORITY_RECENT_CLIPS
    except (TypeError, ValueError):
        return False


def _stable_write_age_seconds_for(row: Dict[str, Any]) -> float:
    base = _stable_write_age_seconds()
    if _is_recent_clips_priority(row):
        return max(base, _recent_clips_stable_write_age_seconds())
    return base


def _clip_has_movement(source_path: str) -> Optional[bool]:
    """快速 SEI peek：判断 RecentClips 片段是否包含车辆移动信号。

    只看 ``SeiMessage.has_movement``（速度 > 0.5 m/s、档位 DRIVE/REVERSE、
    Autopilot 激活），**不依赖 GPS 坐标**——国行车机行车记录 SEI 里没有
    GPS，本函数在国内同样有效。

    返回：
      * ``True`` — 采样到移动信号，片段值得拷贝；
      * ``False`` — 全部静止（或根本没有 SEI），可跳过；
      * ``None`` — 无法判断（解析失败/文件消失等），调用方必须按
        「正常拷贝」处理，绝不静默丢片段。
    """
    try:
        sei_parser = importlib.import_module('web.services.sei_parser')
    except Exception as e:  # noqa: BLE001
        logger.debug("SEI 解析模块不可用（%s），本次跳过静止判断", e)
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
        # 扫完（或触达上限）都没见到移动信号：静止。
        return False
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        # 解析失败：若文件 mtime 已足够旧（Tesla 早已写完），说明从 VFS
        # 视角看这个文件永久不可读——拷贝同样会失败，直接按静止处理，
        # 让队列能排空；否则返回 None 走正常拷贝（数据优先）。
        try:
            age = time.time() - os.stat(source_path).st_mtime
        except OSError:
            age = 0.0
        give_up_age = _peek_give_up_age_seconds()
        if age >= give_up_age:
            logger.warning(
                "SEI peek 失败且文件已静置 %.0fs（阈值 %.0fs）：%s（%s），"
                "按静止处理以便队列排空",
                age, give_up_age, source_path, e,
            )
            return False
        logger.warning("SEI peek 失败：%s（%s），走正常拷贝", source_path, e)
        return None


# ---------------------------------------------------------------------------
# SEI sidecar（拷贝成功后写，供索引器复用，避免二次解析 MP4）
# ---------------------------------------------------------------------------
# sidecar 文件：<视频路径>.sei.json，与视频同目录。
# 只存「有移动信号」的采样消息（与来源 SeiSidecar 设计一致）+ 摘要计数；
# 索引器优先读 sidecar，读不到/校验失败再回退到直接解析 MP4。

_SIDECAR_SUFFIX = '.sei.json'
_SIDECAR_SCHEMA_VERSION = 1
_INLINE_SEI_SAMPLE_RATE = 30
_SIDECAR_WRITE_WARN_SECONDS = 5.0

_SIDECAR_MSG_FIELDS = (
    'frame_index', 'timestamp_ms',
    'vehicle_speed_mps',
    'linear_acceleration_x', 'linear_acceleration_y', 'linear_acceleration_z',
    'steering_wheel_angle', 'accelerator_pedal_position', 'brake_applied',
    'gear_state', 'autopilot_state',
    'blinker_on_left', 'blinker_on_right',
)


def sidecar_path_for(video_path: str) -> str:
    """返回视频对应的 sidecar 路径（``<视频>.sei.json``）。"""
    return video_path + _SIDECAR_SUFFIX


def _message_to_dict(msg: Any) -> Dict[str, Any]:
    """SeiMessage → 可 JSON 序列化的 dict（只取遥测字段）。"""
    out: Dict[str, Any] = {}
    for field in _SIDECAR_MSG_FIELDS:
        out[field] = getattr(msg, field, None)
    return out


def _write_inline_sei_sidecar(dest_path: str) -> Optional[str]:
    """解析刚拷贝好的文件并写 sidecar JSON（含 SEI 摘要）。

    在 ``_atomic_copy`` 之后调用：文件页还在内核页缓存里，解析几乎
    不产生额外 SD 读取。最佳努力——任何失败只记日志、不影响归档
    结果（索引器会回退到直接解析 MP4）。

    成功返回 sidecar 路径，失败返回 None。
    """
    start = time.monotonic()
    try:
        sei_parser = importlib.import_module('web.services.sei_parser')
    except Exception as e:  # noqa: BLE001
        logger.debug("写 SEI sidecar 跳过（解析模块不可用：%s）：%s",
                     e, dest_path)
        return None

    try:
        messages = list(sei_parser.extract_sei_messages(
            dest_path, sample_rate=_INLINE_SEI_SAMPLE_RATE))
    except Exception as e:  # noqa: BLE001
        logger.warning("SEI 解析失败，未写 sidecar：%s（%s），"
                       "索引器将直接解析", dest_path, e)
        return None
    try:
        st = os.stat(dest_path)
    except OSError as e:
        logger.warning("写 SEI sidecar 前 stat 失败：%s（%s）",
                       dest_path, e)
        return None

    moving = [m for m in messages if getattr(m, 'has_movement', False)]

    mvhd_iso = None
    extract_mvhd = getattr(sei_parser, 'extract_mvhd_creation_time', None)
    if extract_mvhd is not None:
        try:
            mvhd = extract_mvhd(dest_path)
            if mvhd is not None:
                mvhd_iso = mvhd.isoformat()
        except Exception as e:  # noqa: BLE001
            logger.debug("读取 mvhd 时间失败：%s（%s）", dest_path, e)

    sidecar = {
        'schema_version': _SIDECAR_SCHEMA_VERSION,
        'sample_rate': _INLINE_SEI_SAMPLE_RATE,
        'sei_count': len(messages),
        'moving_count': len(moving),
        'stationary_count': len(messages) - len(moving),
        'mvhd_time': mvhd_iso,
        'video_size_bytes': st.st_size,
        'video_mtime': st.st_mtime,
        'messages': [_message_to_dict(m) for m in moving],
    }
    sidecar_path = sidecar_path_for(dest_path)
    try:
        tmp_path = sidecar_path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(sidecar, f, ensure_ascii=False)
        os.replace(tmp_path, sidecar_path)
    except OSError as e:
        logger.warning("写 SEI sidecar 文件失败：%s（%s）",
                       dest_path, e)
        return None

    elapsed = time.monotonic() - start
    if elapsed >= _SIDECAR_WRITE_WARN_SECONDS:
        logger.warning(
            "写 SEI sidecar 耗时 %.2fs（阈值 %.1fs）：%s，页缓存快路径"
            "可能失效", elapsed, _SIDECAR_WRITE_WARN_SECONDS,
            os.path.basename(dest_path),
        )
    else:
        logger.info(
            "SEI sidecar 已生成：%s（采样 %d 条，其中移动 %d 条，"
            "mvhd=%s，耗时 %.2fs）",
            os.path.basename(dest_path), len(messages), len(moving),
            mvhd_iso or '未知', elapsed,
        )
    return sidecar_path


def _enqueue_indexed(dest_path: str) -> None:
    """把归档好的目标路径送入索引队列（最佳努力）。"""
    try:
        indexing_worker = importlib.import_module(
            'web.services.indexing_worker')
        enqueue_fn = getattr(indexing_worker, 'enqueue_for_indexing', None)
        if enqueue_fn is None:
            logger.warning("索引队列无入队接口，跳过：%s", dest_path)
            return
        enqueue_fn(None, dest_path, source='archive')
    except Exception as e:  # noqa: BLE001
        # 入队失败不回滚归档：索引器的开机补扫会捡回来。
        logger.warning("送入索引队列失败：%s（%s）", dest_path, e)


def _write_dead_letter_sidecar(archive_root: str,
                               row: Dict[str, Any]) -> None:
    """为 dead_letter 行写诊断文本（最佳努力）。"""
    try:
        sidecar_dir = os.path.join(archive_root, '.dead_letter')
        os.makedirs(sidecar_dir, exist_ok=True)
        sidecar_path = os.path.join(sidecar_dir, f"{row.get('id')}.txt")
        with open(sidecar_path, 'w', encoding='utf-8') as f:
            f.write(f"id: {row.get('id')}\n")
            f.write(f"source_path: {row.get('source_path')}\n")
            f.write(f"dest_path: {row.get('dest_path')}\n")
            f.write(f"attempts: {row.get('attempts')}\n")
            f.write(f"enqueued_at: {row.get('enqueued_at')}\n")
            f.write(f"last_error: {row.get('last_error')}\n")
    except OSError as e:
        logger.warning("写 dead_letter 诊断文件失败 id=%s：%s",
                       row.get('id'), e)


# ---------------------------------------------------------------------------
# 单行处理（无线程，可直接测试）
# ---------------------------------------------------------------------------

def _read_config_or_defaults():
    """读取拷贝调参：chunk 大小 / 最大重试 / 各类 sleep / 负载阈值 / 时间预算。"""
    return (
        int(_cfg_get('archive.copy_chunk_bytes', _DEFAULT_COPY_CHUNK_BYTES)),
        int(_cfg_get('archive.retry_max_attempts', _RETRY_MAX_ATTEMPTS)),
        float(_cfg_get('archive.idle_sleep_seconds', _IDLE_SLEEP_SECONDS)),
        float(_cfg_get('archive.inter_file_sleep_seconds',
                       _INTER_FILE_SLEEP_SECONDS)),
        float(_cfg_get('archive.load_pause_threshold',
                       _LOAD_PAUSE_THRESHOLD)),
        float(_cfg_get('archive.load_pause_seconds', _LOAD_PAUSE_SECONDS)),
        float(_cfg_get('archive.chunk_pause_seconds', _CHUNK_PAUSE_SECONDS)),
        float(_cfg_get('archive.per_file_time_budget_seconds',
                       _PER_FILE_TIME_BUDGET_SECONDS)),
    )


def process_one_claim(row: Dict[str, Any], db_path: str,
                      archive_root: str,
                      teslacam_root: Optional[str], *,
                      chunk_size: int,
                      max_attempts: int,
                      load_pause_threshold: float = 0.0,
                      chunk_pause_seconds: float = 0.25,
                      time_budget_seconds: float = 0.0,
                      now_fn: Callable[[], float] = time.time) -> str:
    """处理一个已认领的行，返回新状态。

    ``'copied'`` / ``'source_gone'`` / ``'skipped_stationary'`` /
    ``'pending'``（释放回队列：写稳定门、磁盘暂停、时间预算、瞬时错误）
    / ``'dead_letter'``（重试用尽）。
    """
    row_id = int(row['id'])
    source_path = row['source_path']

    # 写稳定门：文件太新且 size/mtime 与入队时漂移 → 释放回 pending。
    st = _safe_stat(source_path)
    if st is None:
        mark_source_gone(row_id, db_path=db_path)
        return 'source_gone'
    age = now_fn() - st.st_mtime
    expected_size = row.get('expected_size')
    expected_mtime = row.get('expected_mtime')
    metadata_unknown = (expected_size is None or expected_mtime is None)
    metadata_drifted = (
        (expected_size is not None and expected_size != st.st_size)
        or (expected_mtime is not None and expected_mtime != st.st_mtime)
    )
    if age < _stable_write_age_seconds_for(row) and \
            (metadata_drifted or metadata_unknown):
        release_claim(row_id, expected_size=st.st_size,
                      expected_mtime=st.st_mtime, db_path=db_path)
        return 'pending'

    # 静止跳过：RecentClips 先 SEI peek（跑在写稳定门之后、磁盘守卫之前）。
    # None（无法判断）→ 走正常拷贝，绝不静默丢片段。
    if _is_recent_clips_priority(row):
        movement = _clip_has_movement(source_path)
        if movement is False:
            mark_skipped_stationary(row_id, db_path=db_path)
            logger.info("跳过静止片段（SEI 无移动信号）：%s", source_path)
            return 'skipped_stationary'

    # 磁盘守卫：critical 时释放回 pending 并暂停一段时间。
    global _disk_space_pause_until
    disk_verdict = _check_disk_space_guard(archive_root)
    if disk_verdict == 'critical':
        release_claim(row_id, db_path=db_path)
        pause_s = float(_cfg_get('archive.disk_pause_seconds',
                                 _DISK_PAUSE_SECONDS))
        _disk_space_pause_until = now_fn() + pause_s
        with _state_lock:
            _state['last_disk_pause_at'] = time.time()
        return 'pending'

    try:
        dest_path = compute_dest_path(source_path, archive_root, teslacam_root)
    except ValueError as e:
        mark_failed(row_id, f"目标路径计算失败：{e!r}",
                    max_attempts=max_attempts, db_path=db_path)
        return 'error'

    try:
        _atomic_copy(
            source_path, dest_path, chunk_size,
            load_pause_threshold=load_pause_threshold,
            chunk_pause_seconds=chunk_pause_seconds,
            time_budget_seconds=time_budget_seconds,
            staging_root=_staging_root(archive_root),
        )
    except FileNotFoundError:
        # stat 与 open 之间被 Tesla 轮转掉了：正常，不重试。
        mark_source_gone(row_id, db_path=db_path)
        return 'source_gone'
    except _CopyTimeBudgetExceeded as e:
        # 系统过载信号：释放回 pending，不消耗重试次数。
        logger.warning("拷贝超时放弃（系统过载），稍后重试：%s（%s）",
                       source_path, e)
        release_claim(row_id, db_path=db_path)
        return 'pending'
    except _CopyMoovIncomplete as e:
        # Tesla 还在写（moov 在段尾追加）：顺延，不消耗重试次数。
        defer_count = _bump_moov_defer_count(source_path)
        if defer_count > _MOOV_DEFER_CAP:
            logger.warning(
                "片段 %s 连续 %d 次 moov 缺失（上限 %d），可能已损坏，"
                "转入失败处理：%r", source_path, defer_count,
                _MOOV_DEFER_CAP, e,
            )
            _reset_moov_defer_count(source_path)
            new_status = mark_failed(
                row_id,
                f"拷贝：moov 缺失 {defer_count} 次：{e!r}",
                max_attempts=max_attempts, db_path=db_path,
            )
            if new_status == 'dead_letter':
                row_for_sidecar = dict(row)
                row_for_sidecar['dest_path'] = dest_path
                row_for_sidecar['last_error'] = (
                    f"拷贝：moov 缺失 {defer_count} 次：{e!r}")
                row_for_sidecar['attempts'] = int(row.get('attempts') or 0) + 1
                _write_dead_letter_sidecar(archive_root, row_for_sidecar)
            return new_status
        logger.debug("片段 %s moov 缺失（Tesla 还在写，顺延 %d/%d）",
                     source_path, defer_count, _MOOV_DEFER_CAP)
        st = _safe_stat(source_path)
        if st is None:
            _reset_moov_defer_count(source_path)
            mark_source_gone(row_id, db_path=db_path)
            return 'source_gone'
        release_claim(row_id, expected_size=st.st_size,
                      expected_mtime=st.st_mtime, db_path=db_path)
        return 'pending'
    except (OSError, shutil.Error) as e:
        new_status = mark_failed(
            row_id, f"拷贝失败：{e!r}",
            max_attempts=max_attempts, db_path=db_path,
        )
        if new_status == 'dead_letter':
            row_for_sidecar = dict(row)
            row_for_sidecar['dest_path'] = dest_path
            row_for_sidecar['last_error'] = f"拷贝失败：{e!r}"
            row_for_sidecar['attempts'] = int(row.get('attempts') or 0) + 1
            _write_dead_letter_sidecar(archive_root, row_for_sidecar)
        return new_status

    # 成功：标记 + 写 SEI sidecar + 送入索引队列。
    _reset_moov_defer_count(source_path)
    mark_copied(row_id, dest_path, db_path=db_path)
    try:
        _write_inline_sei_sidecar(dest_path)
    except Exception as e:  # noqa: BLE001
        # sidecar 只是优化，绝不能让归档失败。
        logger.warning("SEI sidecar 异常逃逸（%s），索引器将直接解析：%s",
                       dest_path, e)
    _enqueue_indexed(dest_path)
    return 'copied'


# ---------------------------------------------------------------------------
# 线程生命周期
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_worker_thread: Optional[threading.Thread] = None
_worker_id: Optional[str] = None
_stop_event = threading.Event()
_pause_event = threading.Event()
_idle_event = threading.Event()
_idle_event.set()
_wake_event = threading.Event()
_db_path: Optional[str] = None
_archive_root: Optional[str] = None
_teslacam_root: Optional[str] = None
_state: Dict[str, Any] = {
    'active_file': None,
    'files_done_session': 0,
    'last_drained_at': None,
    'last_error': None,
    'last_outcome': None,
    'last_disk_pause_at': None,
    'last_load_pause_at': None,
    'last_load_pause_loadavg': None,
}
_load_pause_until = 0.0


def _set_state(**fields: Any) -> None:
    with _state_lock:
        _state.update(fields)


def _record_active(file_path: str) -> None:
    with _state_lock:
        _state['active_file'] = file_path
    _idle_event.clear()


def _record_idle(*, last_outcome: Optional[str] = None,
                 last_error: Optional[str] = None) -> None:
    with _state_lock:
        _state['active_file'] = None
        if last_outcome is not None:
            _state['last_outcome'] = last_outcome
        if last_error is not None:
            _state['last_error'] = last_error
    _idle_event.set()


def _is_running() -> bool:
    with _state_lock:
        t = _worker_thread
    return t is not None and t.is_alive()


def is_running() -> bool:
    return _is_running()


def is_paused() -> bool:
    return _pause_event.is_set()


def wake() -> None:
    """唤醒空闲等待中的 worker（生产者入队后调用）。"""
    _wake_event.set()


def start_worker(db_path: str, archive_root: str, *,
                 teslacam_root: Optional[str] = None) -> bool:
    """启动工作线程。幂等：已在运行返回 False。"""
    global _worker_thread, _worker_id, _db_path, _archive_root, _teslacam_root
    global _disk_space_pause_until, _load_pause_until
    with _state_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            logger.warning("归档 worker 已在运行（id=%s），拒绝重复启动",
                           _worker_id)
            return False
        _db_path = db_path
        _archive_root = archive_root
        _teslacam_root = teslacam_root
        _worker_id = f"archive-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        _stop_event.clear()
        _pause_event.clear()
        _wake_event.clear()
        _idle_event.set()
        _state['files_done_session'] = 0
        _state['last_drained_at'] = None
        _state['last_error'] = None
        _state['last_outcome'] = None
        _state['active_file'] = None
        _state['last_disk_pause_at'] = None
        _state['last_load_pause_at'] = None
        _state['last_load_pause_loadavg'] = None
        _disk_space_pause_until = 0.0
        _load_pause_until = 0.0
        thread = threading.Thread(
            target=_run_worker_loop,
            args=(db_path, archive_root, teslacam_root, _worker_id),
            name='archive-worker',
            daemon=True,
        )
        _worker_thread = thread
    thread.start()
    logger.info("归档 worker 已启动（id=%s）", _worker_id)
    return True


def stop_worker(timeout: float = _DEFAULT_STOP_TIMEOUT) -> bool:
    """停止工作线程并等待退出。超时返回 False（此时保留线程引用以阻止
    重启，避免两个线程竞争同一批认领行）。"""
    global _worker_thread
    with _state_lock:
        thread = _worker_thread
    if thread is None:
        return True
    _stop_event.set()
    _pause_event.clear()
    _wake_event.set()
    thread.join(timeout=timeout)
    exited = not thread.is_alive()
    if exited:
        with _state_lock:
            if _worker_thread is thread:
                _worker_thread = None
        logger.info("归档 worker 已干净停止")
    else:
        logger.warning("归档 worker 在 %.1fs 内未退出，保留线程引用以阻止重启",
                       timeout)
    return exited


def pause_worker(timeout: float = _DEFAULT_PAUSE_TIMEOUT) -> bool:
    """在迭代边界暂停（当前文件一定先做完）。

    返回 True 表示 worker 已空闲；False 表示超时仍在处理文件，
    调用方（如模式切换）此时不应继续。
    """
    if not _is_running():
        _pause_event.set()
        return True
    _pause_event.set()
    _wake_event.set()
    became_idle = _idle_event.wait(timeout=timeout)
    if not became_idle:
        logger.warning("归档 worker 在 %.1fs 后仍在处理文件（%s）",
                       timeout, _state.get('active_file'))
    return became_idle


def resume_worker() -> None:
    """解除暂停。"""
    _pause_event.clear()
    _wake_event.set()


def get_status() -> Dict[str, Any]:
    """状态快照（供状态接口 / UI 横幅）。"""
    with _state_lock:
        snap = dict(_state)
        snap['worker_running'] = _is_running()
        snap['worker_id'] = _worker_id
        snap['paused'] = _pause_event.is_set()
        snap['idle'] = _idle_event.is_set()
        db_path = _db_path
    if db_path:
        try:
            snap['queue'] = get_queue_status(db_path)
        except Exception as e:  # noqa: BLE001 — 状态接口绝不抛异常
            logger.warning("读取归档队列状态失败：%s", e)
            snap['queue'] = {}
    return snap


def _apply_low_priority() -> None:
    """把**调用线程**降到最低 CPU/IO 优先级（仅 Linux）。

    注意是线程级：``os.nice(19)`` 会拖累整个进程（含 Flask），
    这里用 ``sched_setscheduler(0, SCHED_IDLE)``（0 = 本线程）+
    ``ionice -c 3 -p <tid>``，只影响本工作线程。
    """
    if not sys.platform.startswith('linux'):
        return
    try:
        SCHED_IDLE = 5
        if hasattr(os, 'sched_setscheduler') and hasattr(os, 'sched_param'):
            os.sched_setscheduler(
                0, SCHED_IDLE, os.sched_param(0),  # type: ignore[attr-defined]
            )
    except (OSError, PermissionError, AttributeError):
        pass
    try:
        import subprocess
        tid = threading.get_native_id()
        subprocess.run(
            ["ionice", "-c", "3", "-p", str(tid)],
            timeout=5, capture_output=True, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired,
            OSError, AttributeError):
        pass


def _wait_with_wake(timeout: float) -> bool:
    """可被 wake() 打断的等待。返回 True 表示被 stop 信号打断。"""
    if _wake_event.wait(timeout=timeout):
        _wake_event.clear()
        return _stop_event.is_set()
    return _stop_event.is_set()


def _run_worker_loop(db_path: str, archive_root: str,
                     teslacam_root: Optional[str],
                     worker_id: str) -> None:
    """线程主体：逐个认领并处理，直到收到停止信号。"""
    global _load_pause_until, _disk_space_pause_until
    _apply_low_priority()
    try:
        stale_age = float(_cfg_get('archive.stale_claim_max_age_seconds',
                                   _STALE_CLAIM_MAX_AGE_SECONDS))
        released = recover_stale_claims(db_path,
                                        max_age_seconds=stale_age)
        if released:
            logger.info("归档 worker 启动：回收了 %d 个陈旧认领", released)
    except Exception as e:  # noqa: BLE001
        logger.warning("回收陈旧认领失败：%s", e)
    try:
        orphans = _sweep_partial_orphans(archive_root)
        if orphans:
            logger.info("归档 worker 启动：清理了 %d 个残留 partial 文件",
                        orphans)
    except Exception as e:  # noqa: BLE001
        logger.warning("清理残留 partial 文件失败：%s", e)

    (chunk_size, max_attempts, idle_sleep, inter_file_sleep,
     load_pause_threshold, load_pause_seconds,
     chunk_pause_seconds, time_budget_seconds) = _read_config_or_defaults()

    while not _stop_event.is_set():
        if _pause_event.is_set():
            _idle_event.set()
            if _stop_event.wait(timeout=inter_file_sleep):
                break
            continue

        # 负载暂停：1 分钟 loadavg 过高时让出 SDIO/看门狗。
        if load_pause_threshold > 0:
            try:
                load1 = os.getloadavg()[0]
            except (AttributeError, OSError):
                load1 = 0.0
            if load1 > load_pause_threshold:
                already_paused = _load_pause_until > time.time()
                _load_pause_until = time.time() + load_pause_seconds
                if not already_paused:
                    with _state_lock:
                        _state['last_load_pause_at'] = time.time()
                        _state['last_load_pause_loadavg'] = float(load1)
                    logger.info(
                        "系统负载 %.2f 超过阈值 %.2f，暂停归档 %.0fs",
                        load1, load_pause_threshold, load_pause_seconds,
                    )
                _idle_event.set()
                if _stop_event.wait(timeout=load_pause_seconds):
                    break
                continue
            elif _load_pause_until > 0 and _load_pause_until <= time.time():
                logger.info("系统负载恢复（%.2f），继续归档", load1)
                _load_pause_until = 0.0

        # 磁盘暂停窗口。
        if _disk_space_pause_until > time.time():
            _idle_event.set()
            remaining = _disk_space_pause_until - time.time()
            if _wait_with_wake(min(remaining, idle_sleep)):
                break
            continue

        row: Optional[Dict[str, Any]] = None
        claim_failed = False
        try:
            try:
                row = claim_next_for_worker(worker_id, db_path=db_path)
            except Exception as e:  # noqa: BLE001
                logger.warning("认领归档任务异常：%s", e)
                _set_state(last_error=f'认领失败：{e!r}')
                claim_failed = True
                row = None

            if row is not None:
                _record_active(row['source_path'])
                try:
                    outcome = process_one_claim(
                        row, db_path, archive_root, teslacam_root,
                        chunk_size=chunk_size,
                        max_attempts=max_attempts,
                        load_pause_threshold=load_pause_threshold,
                        chunk_pause_seconds=chunk_pause_seconds,
                        time_budget_seconds=time_budget_seconds,
                    )
                except Exception as e:  # noqa: BLE001 — 绝不让线程死掉
                    logger.exception("处理 %s 时未预期异常，释放认领",
                                     row['source_path'])
                    try:
                        release_claim(int(row['id']), db_path=db_path)
                    except Exception:  # noqa: BLE001
                        pass
                    _set_state(last_error=f'处理异常：{e!r}')
                    outcome = 'pending'
                _record_idle(last_outcome=outcome)
                if outcome == 'copied':
                    with _state_lock:
                        _state['files_done_session'] += 1
            elif not claim_failed:
                _set_state(last_drained_at=time.time())
        finally:
            _record_idle()

        if claim_failed:
            if _stop_event.wait(timeout=_BACKOFF_SLEEP_SECONDS):
                break
        elif row is None:
            if _wait_with_wake(idle_sleep):
                break
        else:
            if _stop_event.wait(timeout=inter_file_sleep):
                break
