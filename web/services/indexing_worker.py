"""索引工作线程：处理已归档项 → 提取遥测事件。

单线程从 ``indexing_queue`` 逐个认领已归档的 ``.mp4``，解析 SEI
遥测，提取驾驶事件写入 ``detected_events`` 表，供 Web UI 的事件
中心展示。

提取的事件（全部基于 SEI 遥测，**不依赖 GPS**）：
  * ``emergency_brake`` / ``harsh_brake`` —— 纵向加速度 ≤ -7.0 /
    ≤ -4.0 m/s²（急刹/急减速）；
  * ``hard_acceleration`` —— 纵向加速度 ≥ 3.5 m/s²（急加速）；
  * ``sharp_turn`` —— 横向加速度绝对值 ≥ 4.0 m/s²（急转弯）；
  * ``sentry`` / ``saved`` —— SentryClips/SavedClips 事件文件夹级
    条目；优先读 Tesla 的 ``event.json``（触发原因 + 估计坐标），
    读不到时 lat/lon 记为 None（未知≠0.0）；
  * ``sentry_trigger`` —— 哨兵片段的 SEI 显示车辆移动/刹车动作
    （基于速度/加速度/踏板，不依赖 GPS）。

抛弃的来源 legacy 代码：
  * ``trips``/行程表、``waypoints`` 轨迹点写入：全部不移植；
  * ``mapping_service.index_single_file`` 及其 GPS 寻址、行程合并、
    ``purge_deleted_videos`` 等：本模块自带最小索引实现；
  * ``task_coordinator`` 锁竞争：不依赖任务协调器；
  * gps 命名的函数/变量：无；
  * ``clock_skew_repair``：未移植（见任务要求）。

播放时后台让路：``pause_worker()`` 在文件边界暂停，
``resume_worker()`` 恢复；播放器在开始播放前调用暂停。

时区约定：绝不裸用 ``datetime.fromtimestamp()``。事件时间戳 =
文件名时间（车机本地，``tzutil.parse_filename_time``）+
``SeiMessage.timestamp_ms`` 偏移；取不到时回退 ``epoch_to_car(mtime)``。
"""

from __future__ import annotations

import enum
import hashlib
import importlib
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 可调参数（模块级，测试可 monkeypatch）
# ---------------------------------------------------------------------------

_INTER_FILE_SLEEP_SECONDS = 0.25
_IDLE_SLEEP_SECONDS = 1.0
_BACKOFF_SLEEP_SECONDS = 0.5
_DEFAULT_PAUSE_TIMEOUT = 15.0
_DEFAULT_STOP_TIMEOUT = 15.0

# 索引 SEI 采样率：与归档 worker 写 sidecar 时一致。
_INDEX_SEI_SAMPLE_RATE = 30
# 文件新鲜度下限：mtime 在该秒数内的文件本次跳过、稍后重试。
_TOO_NEW_SECONDS = 120.0
# 陈旧认领回收。
_STALE_CLAIM_MAX_AGE_SECONDS = 1800.0
# 解析失败退避：base * 2**attempts，上限 cap。
_PARSE_ERROR_BASE_BACKOFF = 60.0
_PARSE_ERROR_MAX_BACKOFF = 1800.0
_PARSE_ERROR_MAX_ATTEMPTS = 10
# 同类事件去重窗口（秒）。
_EVENT_DEBOUNCE_SECONDS = 5.0

# 事件阈值（m/s²）。
_EMERGENCY_BRAKE_THRESHOLD = -7.0
_HARSH_BRAKE_THRESHOLD = -4.0
_HARD_ACCEL_THRESHOLD = 3.5
_SHARP_TURN_LATERAL_G = 4.0


# ---------------------------------------------------------------------------
# 路径与配置
# ---------------------------------------------------------------------------

def _repo_root() -> str:
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _state_dir() -> str:
    try:
        mod = importlib.import_module('web.services.config')
        getter = getattr(mod, 'get_state_dir', None)
        if getter is not None:
            d = getter()
            if d:
                return str(d)
    except Exception:  # noqa: BLE001
        pass
    return os.path.join(_repo_root(), 'state')


def _default_db_path() -> str:
    return os.path.join(_state_dir(), 'indexing.db')


def _resolve_db_path(db_path: Optional[str]) -> str:
    return db_path if db_path else _default_db_path()


def _cfg_get(dotted_path: str, default: Any) -> Any:
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


def _tz():
    try:
        return importlib.import_module('web.services.tzutil')
    except Exception:  # noqa: BLE001
        return None


def _format_car_time(dt: datetime) -> str:
    tzutil = _tz()
    if tzutil is not None:
        try:
            return tzutil.format_car_time(dt)
        except Exception:  # noqa: BLE001
            pass
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# 索引队列 + 事件表（SQLite，内嵌最小实现）
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS indexing_queue (
    canonical_key   TEXT PRIMARY KEY,
    file_path       TEXT NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 3,
    enqueued_at     REAL NOT NULL,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    source          TEXT NOT NULL DEFAULT 'manual',
    claimed_by      TEXT,
    claimed_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_indexing_queue_ready
    ON indexing_queue(priority, enqueued_at);

CREATE TABLE IF NOT EXISTS detected_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    lat         REAL,
    lon         REAL,
    event_type  TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'info',
    description TEXT NOT NULL,
    video_path  TEXT NOT NULL,
    frame_offset INTEGER NOT NULL DEFAULT 0,
    metadata    TEXT
);
CREATE INDEX IF NOT EXISTS idx_detected_events_video
    ON detected_events(video_path);
CREATE INDEX IF NOT EXISTS idx_detected_events_type
    ON detected_events(event_type);

CREATE TABLE IF NOT EXISTS indexed_files (
    path       TEXT PRIMARY KEY,
    size       INTEGER,
    mtime      REAL,
    indexed_at TEXT NOT NULL,
    outcome    TEXT NOT NULL DEFAULT 'indexed'
);
"""

PRIORITY_EVENTS = 1
PRIORITY_RECENT_CLIPS = 2
PRIORITY_OTHER = 3


def _connect(db_path: Optional[str]) -> sqlite3.Connection:
    path = _resolve_db_path(db_path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(_SCHEMA_SQL)
    return conn


def _canonical_key(file_path: str) -> str:
    norm = os.path.normcase(os.path.abspath(file_path))
    return hashlib.sha1(norm.encode('utf-8', errors='replace')).hexdigest()


def priority_for_path(file_path: str) -> int:
    norm = (file_path or '').replace('\\', '/').lower()
    if '/sentryclips/' in norm or '/savedclips/' in norm:
        return PRIORITY_EVENTS
    if '/recentclips/' in norm:
        return PRIORITY_RECENT_CLIPS
    return PRIORITY_OTHER


def enqueue_for_indexing(db_path: Optional[str], file_path: str, *,
                         priority: Optional[int] = None,
                         source: str = 'manual',
                         next_attempt_at: Optional[float] = None) -> bool:
    """幂等入队待索引文件（归档 worker 成功后调用，source='archive'）。"""
    if not file_path:
        return False
    key = _canonical_key(file_path)
    if priority is None:
        priority = priority_for_path(file_path)
    now = time.time()
    next_at = float(next_attempt_at) if next_attempt_at is not None else 0.0
    conn = None
    try:
        conn = _connect(db_path)
        conn.execute(
            """
            INSERT INTO indexing_queue
                (canonical_key, file_path, priority,
                 enqueued_at, next_attempt_at, source)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_key) DO UPDATE SET
                priority = MIN(priority, excluded.priority),
                file_path = CASE
                    WHEN claimed_by IS NULL THEN excluded.file_path
                    ELSE file_path END,
                source = CASE
                    WHEN claimed_by IS NULL THEN excluded.source
                    ELSE source END
            """,
            (key, file_path, int(priority), now, next_at, source),
        )
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.warning("索引入队失败：%s（%s）", file_path, e)
        return False
    finally:
        if conn is not None:
            conn.close()


def claim_next_queue_item(db_path: Optional[str],
                          worker_id: str) -> Optional[Dict[str, Any]]:
    """原子认领下一个就绪行；无就绪行返回 None。"""
    now = time.time()
    conn = None
    try:
        conn = _connect(db_path)
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute(
                """
                SELECT canonical_key FROM indexing_queue
                 WHERE claimed_by IS NULL
                   AND next_attempt_at <= ?
                   AND attempts < ?
                 ORDER BY priority ASC, enqueued_at ASC
                 LIMIT 1
                """,
                (now, _PARSE_ERROR_MAX_ATTEMPTS),
            ).fetchone()
            if row is None:
                conn.execute('COMMIT')
                return None
            key = row['canonical_key']
            conn.execute(
                """
                UPDATE indexing_queue
                   SET claimed_by = ?, claimed_at = ?
                 WHERE canonical_key = ? AND claimed_by IS NULL
                """,
                (worker_id, now, key),
            )
            full = conn.execute(
                "SELECT * FROM indexing_queue WHERE canonical_key = ?",
                (key,),
            ).fetchone()
            conn.execute('COMMIT')
            if full is None:
                return None
            result = dict(full)
            result['claimed_by'] = worker_id
            result['claimed_at'] = now
            return result
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
    except sqlite3.Error as e:
        logger.warning("认领索引任务失败：%s", e)
        return None
    finally:
        if conn is not None:
            conn.close()


def _guarded_update(db_path: Optional[str], sql: str,
                    params: tuple,
                    row: Dict[str, Any]) -> bool:
    """带认领者守卫的更新：只有认领者未变才生效。"""
    conn = None
    try:
        conn = _connect(db_path)
        claimed_by = row.get('claimed_by')
        claimed_at = row.get('claimed_at')
        if claimed_by is not None and claimed_at is not None:
            sql += " AND claimed_by = ? AND claimed_at = ?"
            params = params + (claimed_by, claimed_at)
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount == 1
    except sqlite3.Error as e:
        logger.warning("索引队列更新失败：%s", e)
        return False
    finally:
        if conn is not None:
            conn.close()


def complete_queue_item(db_path: Optional[str],
                        canonical_key_value: str, *,
                        claimed_by: Optional[str] = None,
                        claimed_at: Optional[float] = None) -> bool:
    """终态完成：删除行。"""
    return _guarded_update(
        db_path,
        "DELETE FROM indexing_queue WHERE canonical_key = ?",
        (canonical_key_value,),
        {'claimed_by': claimed_by, 'claimed_at': claimed_at},
    )


def release_claim(db_path: Optional[str], canonical_key_value: str, *,
                  claimed_by: Optional[str] = None,
                  claimed_at: Optional[float] = None) -> bool:
    """释放认领回待处理（不增加 attempts）。"""
    return _guarded_update(
        db_path,
        """UPDATE indexing_queue
              SET claimed_by = NULL, claimed_at = NULL
            WHERE canonical_key = ?""",
        (canonical_key_value,),
        {'claimed_by': claimed_by, 'claimed_at': claimed_at},
    )


def defer_queue_item(db_path: Optional[str], canonical_key_value: str, *,
                     next_attempt_at: float,
                     bump_attempts: bool = False,
                     last_error: Optional[str] = None,
                     claimed_by: Optional[str] = None,
                     claimed_at: Optional[float] = None) -> bool:
    """推迟到 ``next_attempt_at`` 再试；可选增加 attempts。"""
    attempts_sql = "attempts = attempts + 1," if bump_attempts else ""
    return _guarded_update(
        db_path,
        f"""UPDATE indexing_queue
               SET {attempts_sql}
                   next_attempt_at = ?,
                   last_error = ?,
                   claimed_by = NULL,
                   claimed_at = NULL
             WHERE canonical_key = ?""",
        (float(next_attempt_at), last_error, canonical_key_value),
        {'claimed_by': claimed_by, 'claimed_at': claimed_at},
    )


def recover_stale_claims(db_path: Optional[str],
                         max_age_seconds: float = _STALE_CLAIM_MAX_AGE_SECONDS
                         ) -> int:
    """回收认领超时的行。返回释放数。"""
    conn = None
    try:
        conn = _connect(db_path)
        cutoff = time.time() - float(max_age_seconds)
        cur = conn.execute(
            """
            UPDATE indexing_queue
               SET claimed_by = NULL, claimed_at = NULL
             WHERE claimed_by IS NOT NULL AND claimed_at < ?
            """,
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount
    except sqlite3.Error as e:
        logger.warning("回收索引陈旧认领失败：%s", e)
        return 0
    finally:
        if conn is not None:
            conn.close()


def compute_backoff(attempts: int) -> float:
    """指数退避（秒），有上限。纯函数，便于测试。"""
    if attempts < 0:
        attempts = 0
    return min(_PARSE_ERROR_BASE_BACKOFF * (2 ** attempts),
               _PARSE_ERROR_MAX_BACKOFF)


def get_queue_status(db_path: Optional[str]) -> Dict[str, Any]:
    """队列 + 事件 + 已索引文件统计。"""
    conn = None
    try:
        conn = _connect(db_path)
        now = time.time()
        r = conn.execute(
            """SELECT
                 SUM(CASE WHEN claimed_by IS NULL
                           AND next_attempt_at <= ? THEN 1 ELSE 0 END) AS ready,
                 SUM(CASE WHEN claimed_by IS NULL
                           AND next_attempt_at > ? THEN 1 ELSE 0 END) AS scheduled,
                 SUM(CASE WHEN claimed_by IS NOT NULL THEN 1 ELSE 0 END) AS claimed
               FROM indexing_queue""",
            (now, now),
        ).fetchone()
        events = conn.execute(
            "SELECT COUNT(*) AS n FROM detected_events").fetchone()['n']
        indexed = conn.execute(
            "SELECT COUNT(*) AS n FROM indexed_files").fetchone()['n']
        return {
            'queue_ready': int(r['ready'] or 0),
            'queue_scheduled': int(r['scheduled'] or 0),
            'queue_claimed': int(r['claimed'] or 0),
            'events_total': int(events),
            'indexed_files_total': int(indexed),
        }
    except sqlite3.Error as e:
        logger.warning("读取索引队列状态失败：%s", e)
        return {}
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# 索引结果与事件提取
# ---------------------------------------------------------------------------

class IndexOutcome(enum.Enum):
    INDEXED = 'indexed'
    ALREADY_INDEXED = 'already_indexed'
    NO_MOVEMENT = 'no_movement'
    FILE_MISSING = 'file_missing'
    TOO_NEW = 'too_new'
    PARSE_ERROR = 'parse_error'
    DB_BUSY = 'db_busy'


@dataclass(frozen=True)
class IndexResult:
    outcome: IndexOutcome
    error: Optional[str] = None


@dataclass(frozen=True)
class WorkerAction:
    """处理一个已认领行后的决策（纯值对象，便于测试）。"""
    action: str  # 'complete' | 'defer' | 'release'
    next_attempt_at: Optional[float] = None
    bump_attempts: bool = False
    purge_path: Optional[str] = None
    last_error: Optional[str] = None
    outcome: Optional[IndexOutcome] = None


def _thresholds() -> Dict[str, float]:
    return {
        'emergency_brake_threshold': float(_cfg_get(
            'indexing.emergency_brake_threshold', _EMERGENCY_BRAKE_THRESHOLD)),
        'harsh_brake_threshold': float(_cfg_get(
            'indexing.harsh_brake_threshold', _HARSH_BRAKE_THRESHOLD)),
        'hard_accel_threshold': float(_cfg_get(
            'indexing.hard_accel_threshold', _HARD_ACCEL_THRESHOLD)),
        'sharp_turn_lateral_g': float(_cfg_get(
            'indexing.sharp_turn_lateral_g', _SHARP_TURN_LATERAL_G)),
    }


def _iter_sei_messages(file_path: str) -> List[Any]:
    """取 SEI 消息列表：优先读归档 worker 写的 sidecar，回退直接解析。

    sidecar 不可用/校验失败时回退 ``extract_sei_messages`` /
    ``parse_video_sei``；解析失败抛异常，由调用方转为 PARSE_ERROR。
    """
    cached = _read_sei_sidecar(file_path)
    if cached is not None:
        return cached

    sei_parser = importlib.import_module('web.services.sei_parser')

    extract = getattr(sei_parser, 'extract_sei_messages', None)
    if extract is not None:
        return list(extract(file_path,
                            sample_rate=_INDEX_SEI_SAMPLE_RATE))

    parse = getattr(sei_parser, 'parse_video_sei', None)
    if parse is not None:
        result = parse(file_path)
        messages = getattr(result, 'messages', None)
        if messages:
            return list(messages)
        return []

    raise RuntimeError("SEI 解析模块无可用解析接口")


def _read_sei_sidecar(file_path: str) -> Optional[List[Any]]:
    """读归档 worker 写的 ``<视频>.sei.json``。

    校验 ``video_size_bytes`` / ``video_mtime``，漂移则视为失效。
    返回消息对象列表（dict 转 SimpleNamespace）；无 sidecar 或校验
    失败返回 None（调用方回退到直接解析）。
    """
    path = file_path + '.sei.json'
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        return None
    try:
        st = os.stat(file_path)
    except OSError:
        return None
    if (data.get('video_size_bytes') != st.st_size
            or data.get('video_mtime') != st.st_mtime):
        logger.debug("SEI sidecar 校验失败（文件已变化）：%s", file_path)
        return None
    messages = data.get('messages') or []
    return [SimpleNamespace(**m) if isinstance(m, dict) else m
            for m in messages]


def _file_base_time(file_path: str) -> datetime:
    """片段基准时间：文件名时间（车机本地）优先，回退 mtime。"""
    tzutil = _tz()
    name = os.path.basename(file_path)
    if tzutil is not None:
        parse_fn = getattr(tzutil, 'parse_filename_time', None)
        if parse_fn is not None:
            try:
                dt = parse_fn(name)
                if dt is not None:
                    return dt
            except Exception:  # noqa: BLE001
                pass
        epoch_fn = getattr(tzutil, 'epoch_to_car', None)
        if epoch_fn is not None:
            try:
                return epoch_fn(os.path.getmtime(file_path))
            except Exception:  # noqa: BLE001
                pass
    # 最后兜底：UTC（绝不依赖系统本地时区）。
    try:
        return datetime.fromtimestamp(os.path.getmtime(file_path),
                                      tz=timezone.utc)
    except OSError:
        return datetime.now(timezone.utc)


def _detect_motion_events(messages: List[Any], base: datetime,
                          video_path: str,
                          thresholds: Dict[str, float]) -> List[dict]:
    """对 SEI 消息做规则事件检测（急刹/急加速/急转弯）。

    不依赖 GPS：只用加速度与速度。lat/lon 恒为 None。

    注意：去重基于完整精度的 datetime（``tzutil.format_car_time``
    只到分钟精度，不能用于 5 秒去重窗口的比较）。
    """
    staged: List[tuple] = []  # (dt, event-dict)
    for msg in messages:
        ax = float(getattr(msg, 'linear_acceleration_x', 0.0) or 0.0)
        ay = float(getattr(msg, 'linear_acceleration_y', 0.0) or 0.0)
        speed = float(getattr(msg, 'vehicle_speed_mps', 0.0) or 0.0)
        ts_ms = float(getattr(msg, 'timestamp_ms', 0.0) or 0.0)
        frame = int(getattr(msg, 'frame_index',
                            getattr(msg, 'frame_seq_no', 0)) or 0)
        dt = base + timedelta(milliseconds=ts_ms)
        meta_base = {'accel_x': ax, 'accel_y': ay, 'speed_mps': speed}

        if ax <= thresholds['emergency_brake_threshold']:
            staged.append((dt, {
                'lat': None, 'lon': None,
                'event_type': 'emergency_brake', 'severity': 'critical',
                'description': f"急刹车：纵向加速度 {ax:.1f} m/s²",
                'video_path': video_path, 'frame_offset': frame,
                'metadata': json.dumps(meta_base, ensure_ascii=False),
            }))
        elif ax <= thresholds['harsh_brake_threshold']:
            staged.append((dt, {
                'lat': None, 'lon': None,
                'event_type': 'harsh_brake', 'severity': 'warning',
                'description': f"急减速：纵向加速度 {ax:.1f} m/s²",
                'video_path': video_path, 'frame_offset': frame,
                'metadata': json.dumps(meta_base, ensure_ascii=False),
            }))
        if ax >= thresholds['hard_accel_threshold']:
            staged.append((dt, {
                'lat': None, 'lon': None,
                'event_type': 'hard_acceleration', 'severity': 'info',
                'description': f"急加速：纵向加速度 {ax:.1f} m/s²",
                'video_path': video_path, 'frame_offset': frame,
                'metadata': json.dumps(meta_base, ensure_ascii=False),
            }))
        if abs(ay) >= thresholds['sharp_turn_lateral_g']:
            staged.append((dt, {
                'lat': None, 'lon': None,
                'event_type': 'sharp_turn', 'severity': 'warning',
                'description': f"急转弯：横向加速度 {ay:.1f} m/s²",
                'video_path': video_path, 'frame_offset': frame,
                'metadata': json.dumps(
                    {'accel_y': ay, 'speed_mps': speed}, ensure_ascii=False),
            }))

    events = []
    for dt, ev in _debounce_datetimes(
            staged, window_seconds=_EVENT_DEBOUNCE_SECONDS):
        ev['timestamp'] = _format_car_time(dt)
        events.append(ev)
    return events


def _debounce_datetimes(staged: List[tuple],
                        window_seconds: float = 5.0) -> List[tuple]:
    """同类事件在时间窗口内只保留第一个（基于完整精度 datetime）。"""
    if not staged:
        return staged
    result = []
    last_by_type: Dict[str, datetime] = {}
    for dt, ev in staged:
        key = ev['event_type']
        if key in last_by_type:
            try:
                delta = abs((dt - last_by_type[key]).total_seconds())
            except (TypeError, ValueError):
                delta = window_seconds
            if delta < window_seconds:
                continue  # 窗口内重复，丢弃
        result.append((dt, ev))
        last_by_type[key] = dt
    return result


def _read_event_json(event_folder: str) -> Optional[dict]:
    """读 Tesla 事件文件夹里的 event.json（触发原因/估计坐标）。"""
    ej = os.path.join(event_folder, 'event.json')
    try:
        if not os.path.isfile(ej):
            return None
        with open(ej, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    out: Dict[str, Any] = {'reason': data.get('reason') or '未知原因'}
    try:
        lat = float(data.get('est_lat'))
        lon = float(data.get('est_lon'))
        import math
        if (math.isfinite(lat) and math.isfinite(lon)
                and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0
                and not (lat == 0 and lon == 0)):
            out['lat'], out['lon'] = lat, lon
        else:
            out['lat'] = out['lon'] = None
    except (TypeError, ValueError):
        out['lat'] = out['lon'] = None
    return out


def _sentry_folder_events(file_path: str, teslacam_root: Optional[str],
                          messages: List[Any], base: datetime) -> List[dict]:
    """为 SentryClips/SavedClips 片段生成事件文件夹级事件。

    * ``sentry``/``saved``：每个事件文件夹一条（去重在入库时做）；
      坐标优先取 event.json，取不到为 None；
    * ``sentry_trigger``：SEI 显示车辆移动/刹车动作（基于速度/
      加速度/踏板，不依赖 GPS）。
    """
    norm = file_path.replace('\\', '/')
    is_sentry = '/SentryClips/' in norm
    is_saved = '/SavedClips/' in norm
    if not (is_sentry or is_saved):
        return []
    event_folder = os.path.dirname(file_path)
    folder_name = os.path.basename(event_folder)
    ts = _format_car_time(base)

    lat = lon = None
    reason = '未知原因'
    if teslacam_root:
        # 事件文件夹名形如 2026-10-07_09-15-32；源目录结构与归档一致。
        for sub in ('SentryClips', 'SavedClips'):
            cand = os.path.join(teslacam_root, sub, folder_name)
            ej = _read_event_json(cand)
            if ej is not None:
                lat, lon, reason = ej.get('lat'), ej.get('lon'), ej['reason']
                break
    else:
        # 归档侧没有 teslacam_root 时，试试片段同目录的 event.json
        # （某些手动导入场景）。
        ej = _read_event_json(event_folder)
        if ej is not None:
            lat, lon, reason = ej.get('lat'), ej.get('lon'), ej['reason']

    event_type = 'sentry' if is_sentry else 'saved'
    label = '哨兵模式' if is_sentry else '手动保存'
    events = [{
        'timestamp': ts, 'lat': lat, 'lon': lon,
        'event_type': event_type,
        'severity': 'warning' if is_sentry else 'info',
        'description': f"{label}触发（{reason}）",
        'video_path': file_path, 'frame_offset': 0,
        'metadata': json.dumps(
            {'event_folder': folder_name, 'reason': reason},
            ensure_ascii=False),
    }]

    # 哨兵触发时的车辆动作：速度/加速度/踏板任一有信号即记一条。
    trigger = None
    for msg in messages:
        if getattr(msg, 'has_movement', False):
            trigger = '检测到车辆移动'
            break
        if getattr(msg, 'brake_applied', False):
            trigger = '检测到刹车动作'
            break
    if trigger and is_sentry:
        events.append({
            'timestamp': ts, 'lat': lat, 'lon': lon,
            'event_type': 'sentry_trigger', 'severity': 'warning',
            'description': f"哨兵触发：{trigger}",
            'video_path': file_path, 'frame_offset': 0,
            'metadata': json.dumps(
                {'event_folder': folder_name, 'reason': reason},
                ensure_ascii=False),
        })
    return events


def _store_events(db_path: Optional[str], events: List[dict]) -> int:
    """入库事件；sentry/saved 按事件文件夹去重。返回写入数。"""
    if not events:
        return 0
    conn = None
    try:
        conn = _connect(db_path)
        n = 0
        for ev in events:
            if ev['event_type'] in ('sentry', 'saved'):
                try:
                    folder = json.loads(ev['metadata'] or '{}').get(
                        'event_folder')
                except (ValueError, TypeError):
                    folder = None
                if folder:
                    exists = conn.execute(
                        """SELECT 1 FROM detected_events
                            WHERE event_type = ?
                              AND metadata LIKE ?
                            LIMIT 1""",
                        (ev['event_type'], f'%"event_folder": "{folder}"%'),
                    ).fetchone()
                    if exists:
                        continue
            conn.execute(
                """INSERT INTO detected_events
                       (timestamp, lat, lon, event_type, severity,
                        description, video_path, frame_offset, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ev['timestamp'], ev['lat'], ev['lon'],
                 ev['event_type'], ev['severity'], ev['description'],
                 ev['video_path'], ev['frame_offset'], ev['metadata']),
            )
            n += 1
        conn.commit()
        return n
    except sqlite3.Error as e:
        logger.warning("事件入库失败：%s", e)
        return 0
    finally:
        if conn is not None:
            conn.close()


def _mark_indexed(db_path: Optional[str], file_path: str,
                  outcome: IndexOutcome) -> None:
    conn = None
    try:
        conn = _connect(db_path)
        try:
            st = os.stat(file_path)
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = None, None
        conn.execute(
            """INSERT INTO indexed_files
                   (path, size, mtime, indexed_at, outcome)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                   size = excluded.size,
                   mtime = excluded.mtime,
                   indexed_at = excluded.indexed_at,
                   outcome = excluded.outcome""",
            (file_path, size, mtime, _format_car_time(
                datetime.now(timezone.utc)), outcome.value),
        )
        conn.commit()
    except sqlite3.Error as e:
        logger.warning("标记已索引失败：%s（%s）", file_path, e)
    finally:
        if conn is not None:
            conn.close()


def _already_indexed(db_path: Optional[str], file_path: str) -> bool:
    conn = None
    try:
        conn = _connect(db_path)
        row = conn.execute(
            "SELECT size, mtime FROM indexed_files WHERE path = ?",
            (file_path,),
        ).fetchone()
        if row is None:
            return False
        try:
            st = os.stat(file_path)
        except OSError:
            return False
        return row['size'] == st.st_size and row['mtime'] == st.st_mtime
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            conn.close()


def _purge_path_index(db_path: Optional[str], file_path: str) -> None:
    """删除某路径的事件与已索引记录（源文件消失时）。"""
    conn = None
    try:
        conn = _connect(db_path)
        conn.execute("DELETE FROM detected_events WHERE video_path = ?",
                     (file_path,))
        conn.execute("DELETE FROM indexed_files WHERE path = ?",
                     (file_path,))
        conn.commit()
    except sqlite3.Error as e:
        logger.warning("清理路径索引失败：%s（%s）", file_path, e)
    finally:
        if conn is not None:
            conn.close()


def _index_single_file(file_path: str, db_path: Optional[str],
                       teslacam_root: Optional[str]) -> IndexResult:
    """索引单个已归档文件：SEI → 事件 → 入库。"""
    try:
        mtime = os.path.getmtime(file_path)
    except OSError:
        return IndexResult(IndexOutcome.FILE_MISSING, "源文件不存在")

    if time.time() - mtime < _TOO_NEW_SECONDS:
        return IndexResult(IndexOutcome.TOO_NEW, "文件太新，稍后重试")

    if _already_indexed(db_path, file_path):
        return IndexResult(IndexOutcome.ALREADY_INDEXED)

    try:
        messages = _iter_sei_messages(file_path)
    except ImportError as e:
        return IndexResult(IndexOutcome.PARSE_ERROR,
                           f"SEI 解析模块不可用：{e}")
    except Exception as e:  # noqa: BLE001
        return IndexResult(IndexOutcome.PARSE_ERROR, f"SEI 解析失败：{e}")

    base = _file_base_time(file_path)
    thresholds = _thresholds()
    events = _detect_motion_events(messages, base, file_path, thresholds)
    events += _sentry_folder_events(file_path, teslacam_root, messages, base)

    if not messages:
        # 文件可解析但无 SEI：视为无移动信号，记完成（不再反复解析）。
        _mark_indexed(db_path, file_path, IndexOutcome.NO_MOVEMENT)
        logger.info("索引完成（无 SEI/无移动信号）：%s",
                    os.path.basename(file_path))
        return IndexResult(IndexOutcome.NO_MOVEMENT)

    try:
        n = _store_events(db_path, events)
    except sqlite3.OperationalError as e:
        if 'locked' in str(e).lower() or 'busy' in str(e).lower():
            return IndexResult(IndexOutcome.DB_BUSY, f"数据库忙：{e}")
        return IndexResult(IndexOutcome.PARSE_ERROR, f"事件入库失败：{e}")
    except Exception as e:  # noqa: BLE001
        return IndexResult(IndexOutcome.PARSE_ERROR, f"事件入库失败：{e}")

    _mark_indexed(db_path, file_path, IndexOutcome.INDEXED)
    logger.info("索引完成：%s（事件 %d 个）",
                os.path.basename(file_path), n)
    return IndexResult(IndexOutcome.INDEXED)


# ---------------------------------------------------------------------------
# 分发（纯函数，可直接测试）
# ---------------------------------------------------------------------------

def process_claimed_item(
    row: Dict[str, Any],
    db_path: Optional[str],
    teslacam_root: Optional[str],
    *,
    indexer: Optional[Callable[..., IndexResult]] = None,
    now_fn: Callable[[], float] = time.time,
) -> WorkerAction:
    """对已认领行做决策，返回 WorkerAction（线程负责真正执行）。

    indexer 异常一律转为带退避的 defer，绝不杀死线程。
    """
    if indexer is None:
        indexer = _index_single_file
    file_path = row['file_path']
    attempts = int(row.get('attempts') or 0)

    try:
        result = indexer(file_path, db_path, teslacam_root)
    except Exception as e:  # noqa: BLE001
        logger.exception("索引器对 %s 抛异常，转入退避重试", file_path)
        return WorkerAction(
            action='defer',
            next_attempt_at=now_fn() + compute_backoff(attempts),
            bump_attempts=True,
            last_error=f'未处理异常：{e!r}',
            outcome=IndexOutcome.PARSE_ERROR,
        )

    outcome = result.outcome
    if outcome in (IndexOutcome.INDEXED, IndexOutcome.ALREADY_INDEXED,
                   IndexOutcome.NO_MOVEMENT):
        return WorkerAction(action='complete', outcome=outcome)
    if outcome == IndexOutcome.FILE_MISSING:
        return WorkerAction(action='complete', purge_path=file_path,
                            outcome=outcome)
    if outcome == IndexOutcome.TOO_NEW:
        try:
            mtime = os.path.getmtime(file_path)
        except OSError:
            return WorkerAction(action='complete', purge_path=file_path,
                                outcome=IndexOutcome.FILE_MISSING)
        # +125s：留 5s 余量，避免下次认领立刻再次 TOO_NEW。
        return WorkerAction(action='defer', next_attempt_at=mtime + 125.0,
                            bump_attempts=False, outcome=outcome)
    if outcome == IndexOutcome.PARSE_ERROR:
        return WorkerAction(
            action='defer',
            next_attempt_at=now_fn() + compute_backoff(attempts),
            bump_attempts=True, last_error=result.error, outcome=outcome,
        )
    if outcome == IndexOutcome.DB_BUSY:
        # 不增加 attempts：纯属数据库忙，下次直接重试。
        return WorkerAction(action='release', last_error=result.error,
                            outcome=outcome)
    logger.warning("process_claimed_item: 未知结果 %r（%s）",
                   outcome, file_path)
    return WorkerAction(action='release', outcome=outcome)


def _apply_action(action: WorkerAction, row: Dict[str, Any],
                  db_path: Optional[str]) -> None:
    """执行 WorkerAction（带认领者守卫）。"""
    key = row['canonical_key']
    claimed_by = row.get('claimed_by')
    claimed_at = row.get('claimed_at')
    if action.action == 'complete':
        complete_queue_item(db_path, key,
                            claimed_by=claimed_by, claimed_at=claimed_at)
        if action.purge_path:
            _purge_path_index(db_path, action.purge_path)
    elif action.action == 'defer':
        defer_queue_item(
            db_path, key,
            next_attempt_at=action.next_attempt_at or 0.0,
            bump_attempts=action.bump_attempts,
            last_error=action.last_error,
            claimed_by=claimed_by, claimed_at=claimed_at,
        )
    elif action.action == 'release':
        release_claim(db_path, key,
                      claimed_by=claimed_by, claimed_at=claimed_at)
    else:
        logger.warning("未知 WorkerAction：%r", action.action)


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
_db_path: Optional[str] = None
_teslacam_root: Optional[str] = None
_state: Dict[str, Any] = {
    'active_file': None,
    'active_canonical_key': None,
    'source': None,
    'files_done_session': 0,
    'last_drained_at': None,
    'last_error': None,
    'last_outcome': None,
}


def _set_worker_state(**fields: Any) -> None:
    with _state_lock:
        _state.update(fields)


def _record_active(file_path: str, source: Optional[str],
                   canonical_key_value: str) -> None:
    with _state_lock:
        _state['active_file'] = file_path
        _state['source'] = source
        _state['active_canonical_key'] = canonical_key_value
    _idle_event.clear()


def _record_idle(last_outcome: Optional[IndexOutcome] = None,
                 last_error: Optional[str] = None) -> None:
    with _state_lock:
        _state['active_file'] = None
        _state['source'] = None
        _state['active_canonical_key'] = None
        if last_outcome is not None:
            _state['last_outcome'] = last_outcome.value
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


def start_worker(db_path: Optional[str],
                 teslacam_root: Optional[str]) -> bool:
    """启动索引线程。幂等。"""
    global _worker_thread, _worker_id, _db_path, _teslacam_root
    with _state_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            logger.warning("索引 worker 已在运行（id=%s），拒绝重复启动",
                           _worker_id)
            return False
        _db_path = db_path
        _teslacam_root = teslacam_root
        _worker_id = f"index-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        _stop_event.clear()
        _pause_event.clear()
        _idle_event.set()
        _state['files_done_session'] = 0
        _state['last_drained_at'] = None
        _state['last_error'] = None
        _state['last_outcome'] = None
        thread = threading.Thread(
            target=_run_worker_loop,
            args=(db_path, teslacam_root, _worker_id),
            name='indexing-worker',
            daemon=True,
        )
        _worker_thread = thread
    thread.start()
    logger.info("索引 worker 已启动（id=%s）", _worker_id)
    return True


def stop_worker(timeout: float = _DEFAULT_STOP_TIMEOUT) -> bool:
    """停止索引线程。超时返回 False（保留线程引用以阻止重启）。"""
    global _worker_thread
    with _state_lock:
        thread = _worker_thread
    if thread is None:
        return True
    _stop_event.set()
    _pause_event.clear()
    thread.join(timeout=timeout)
    exited = not thread.is_alive()
    if exited:
        with _state_lock:
            if _worker_thread is thread:
                _worker_thread = None
        logger.info("索引 worker 已干净停止")
    else:
        logger.warning("索引 worker 在 %.1fs 内未退出，保留线程引用以阻止重启",
                       timeout)
    return exited


def pause_worker(timeout: float = _DEFAULT_PAUSE_TIMEOUT) -> bool:
    """在文件边界暂停（视频播放前调用，让出 IO/CPU）。

    True=已空闲；False=超时仍在处理单个文件，调用方不应继续。
    """
    if not _is_running():
        _pause_event.set()
        return True
    _pause_event.set()
    became_idle = _idle_event.wait(timeout=timeout)
    if not became_idle:
        logger.warning("索引 worker 在 %.1fs 后仍在处理文件（%s）",
                       timeout, _state.get('active_file'))
    return became_idle


def resume_worker() -> None:
    """解除暂停。"""
    _pause_event.clear()


def get_worker_status() -> Dict[str, Any]:
    """状态快照（供 /api/index/status 与 UI 横幅）。"""
    with _state_lock:
        snap = {
            'worker_running': _is_running(),
            'worker_id': _worker_id,
            'paused': _pause_event.is_set(),
            'idle': _idle_event.is_set(),
            'active_file': _state['active_file'],
            'source': _state['source'],
            'files_done_session': _state['files_done_session'],
            'last_drained_at': _state['last_drained_at'],
            'last_error': _state['last_error'],
            'last_outcome': _state['last_outcome'],
        }
        db_path = _db_path
    if db_path is not None:
        try:
            snap.update(get_queue_status(db_path))
        except Exception as e:  # noqa: BLE001 — 状态接口绝不抛异常
            logger.warning("读取索引队列状态失败：%s", e)
    return snap


def _apply_low_priority() -> None:
    """把**调用线程**降到最低 CPU/IO 优先级（仅 Linux，线程级）。"""
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


def _process_one(row: Dict[str, Any], db_path: Optional[str],
                 teslacam_root: Optional[str]) -> None:
    """处理一个已认领行。任何异常都转为释放认领，线程不死。"""
    key = row['canonical_key']
    file_path = row['file_path']
    source = row.get('source')

    _record_active(file_path, source, key)
    try:
        action = process_claimed_item(row, db_path, teslacam_root)
        _apply_action(action, row, db_path)
        with _state_lock:
            if action.outcome == IndexOutcome.INDEXED:
                _state['files_done_session'] += 1
            if action.outcome is not None:
                _state['last_outcome'] = action.outcome.value
            if action.last_error:
                _state['last_error'] = action.last_error
            elif action.action == 'complete':
                _state['last_error'] = None
    except Exception as e:  # noqa: BLE001
        logger.exception("索引分发出错 %s，释放认领", file_path)
        try:
            release_claim(db_path, key,
                          claimed_by=row.get('claimed_by'),
                          claimed_at=row.get('claimed_at'))
        except Exception:  # noqa: BLE001
            pass
        _set_worker_state(last_error=f'分发异常：{e!r}')
    finally:
        _record_idle()


def _run_worker_loop(db_path: Optional[str],
                     teslacam_root: Optional[str],
                     worker_id: str) -> None:
    """线程主体：逐个认领，直到停止信号。"""
    _apply_low_priority()
    try:
        released = recover_stale_claims(db_path)
        if released:
            logger.info("索引 worker 启动：回收了 %d 个陈旧认领", released)
    except Exception as e:  # noqa: BLE001
        logger.warning("回收陈旧认领失败：%s", e)

    while not _stop_event.is_set():
        if _pause_event.is_set():
            _idle_event.set()
            if _stop_event.wait(timeout=_INTER_FILE_SLEEP_SECONDS):
                break
            continue

        row: Optional[Dict[str, Any]] = None
        claim_failed = False
        try:
            try:
                row = claim_next_queue_item(db_path, worker_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("认领索引任务异常：%s", e)
                _set_worker_state(last_error=f'认领失败：{e!r}')
                claim_failed = True
                row = None

            if row is not None:
                _process_one(row, db_path, teslacam_root)
            elif not claim_failed:
                _set_worker_state(last_drained_at=time.time())
        finally:
            _record_idle()

        if claim_failed:
            if _stop_event.wait(timeout=_BACKOFF_SLEEP_SECONDS):
                break
        elif row is None:
            if _stop_event.wait(timeout=_IDLE_SLEEP_SECONDS):
                break
        else:
            if _stop_event.wait(timeout=_INTER_FILE_SLEEP_SECONDS):
                break
