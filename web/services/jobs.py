"""后台长耗时任务管理（线程实现，供备份 / 清理 / 上传等操作上报进度）。

约定：
- ``target`` 签名：``target(progress, *args, **kwargs)``，其中
  ``progress(percent: int, message: str)`` 为进度回调，``message`` 一律中文。
- 任务状态：``running`` / ``done`` / ``failed`` / ``cancelled``。
- 异常被捕获记为 ``failed``（message 记中文原因），不向外抛。
- 取消是协作式的：``progress`` 回调在收到取消请求时抛内部异常，
  任务应在耗时循环中定期调用 ``progress``；从不调用 ``progress`` 的
  任务无法被及时取消（``cancel_job`` 仍返回 True，但线程会跑完）。

纯 Python + 标准库，不依赖 Flask；线程安全。
"""

import itertools
import logging
import threading
from uuid import uuid4

logger = logging.getLogger(__name__)

# 任务状态常量
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_UNKNOWN = "unknown"

_MAX_STORED_JOBS = 200

_lock = threading.Lock()
_counter = itertools.count(1)
# job_id -> 记录 dict（含内部字段 cancel_event / thread，不对外暴露）
_jobs = {}


class _JobCancelled(Exception):
    """内部异常：任务收到取消请求时由 progress 回调抛出。"""


def _public_view(record: dict) -> dict:
    return {
        "job_id": record["job_id"],
        "name": record["name"],
        "state": record["state"],
        "percent": record["percent"],
        "message": record["message"],
    }


def _finish(record: dict, state: str, percent: int, message: str) -> None:
    with _lock:
        record["state"] = state
        record["percent"] = max(0, min(100, int(percent)))
        record["message"] = message


def _evict_finished_locked() -> None:
    """存储上限保护：淘汰最早结束的任务（只淘汰非 running 的）。"""
    if len(_jobs) <= _MAX_STORED_JOBS:
        return
    finished = [jid for jid, r in _jobs.items() if r["state"] != STATE_RUNNING]
    # dict 保持插入顺序：最早结束的在前
    for jid in finished[: len(_jobs) - _MAX_STORED_JOBS]:
        del _jobs[jid]


def start_job(name: str, target, *args, **kwargs) -> str:
    """启动一个后台任务，返回 job_id。

    Args:
        name: 任务名（中文，如“备份到 NAS”），用于展示与 job_id 前缀。
        target: ``target(progress, *args, **kwargs)``；``progress(percent, message)``
            中 ``message`` 应为中文。
    """
    job_id = f"{name}-{next(_counter)}-{uuid4().hex[:6]}"
    cancel_event = threading.Event()
    record = {
        "job_id": job_id,
        "name": name,
        "state": STATE_RUNNING,
        "percent": 0,
        "message": "准备中",
        "cancel_event": cancel_event,
        "thread": None,
    }

    def progress(percent: int, message: str) -> None:
        if cancel_event.is_set():
            raise _JobCancelled()
        with _lock:
            record["percent"] = max(0, min(100, int(percent)))
            record["message"] = str(message)

    def _runner() -> None:
        try:
            target(progress, *args, **kwargs)
        except _JobCancelled:
            with _lock:
                pct = record["percent"]
            _finish(record, STATE_CANCELLED, pct, "已取消")
            logger.info("任务已取消：%s", job_id)
        except Exception as e:  # noqa: BLE001 — 任务异常一律记 failed，不外抛
            logger.exception("任务失败：%s", job_id)
            _finish(record, STATE_FAILED, record["percent"], f"失败：{e}")
        else:
            with _lock:
                last_message = record["message"]
            final_message = last_message if last_message != "准备中" else "完成"
            _finish(record, STATE_DONE, 100, final_message)
            logger.info("任务完成：%s", job_id)

    thread = threading.Thread(target=_runner, name=f"job-{job_id}", daemon=True)
    record["thread"] = thread
    with _lock:
        _jobs[job_id] = record
        _evict_finished_locked()
    thread.start()
    logger.info("任务启动：%s", job_id)
    return job_id


def get_job_progress(job_id: str) -> dict:
    """返回任务进度 ``{job_id, name, state, percent, message}``。

    job_id 不存在时返回 ``state="unknown"`` 的记录，不抛异常。
    """
    with _lock:
        record = _jobs.get(job_id)
        if record is None:
            return {
                "job_id": job_id,
                "name": "",
                "state": STATE_UNKNOWN,
                "percent": 0,
                "message": "任务不存在",
            }
        return _public_view(record)


def cancel_job(job_id: str) -> bool:
    """请求取消任务。任务存在且正在运行时返回 True，否则返回 False。

    取消是协作式的：实际停止发生在任务下一次调用 ``progress`` 时。
    """
    with _lock:
        record = _jobs.get(job_id)
        if record is None or record["state"] != STATE_RUNNING:
            return False
        record["cancel_event"].set()
        return True
