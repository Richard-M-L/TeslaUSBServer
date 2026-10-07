"""系统状态 / 存储 / 模式切换 / 日志服务。

接口（docs/INTERFACES.md）::

    get_status()   -> {mode: present/edit, temp_c, throttled: bool,
                       wifi: {ssid, signal}, version}
    get_storage()  -> {teslacam: {total_gb, used_gb}, lightshow: {...}}
    switch_mode(target)  # 调用方二次确认，服务层只执行
    get_logs(level=None) / download_logs() / clear_logs()

另提供 ``log(level, msg)`` 及 ``log_info / log_warning / log_error /
log_debug``，供其他服务写入中文日志：统一落盘到 ``<state>/app.log``，
时间戳使用车机时区（tzutil，时区 P0 规范）。

纯标准库，不依赖 Flask。
"""

import io
import logging
import os
import re
import shutil
import subprocess
import threading
import zipfile

from .config import get_mount_dir, get_state_dir
from .tzutil import format_car_time, now_car

logger = logging.getLogger(__name__)

VERSION = "v1.0-cn"

# 仓库根（本文件位于 <repo>/web/services/）
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")

_VALID_MODES = ("present", "edit")

_LOG_LOCK = threading.Lock()
_MAX_LOG_BYTES = 2 * 1024 * 1024  # 日志超过 2MB 时截断，只保留后半部分

# 日志行格式：2026-10-07 11:22 [info] 内容（时间戳由 tzutil 生成，车机时区）
_LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?)\s*\[(\w+)\]\s*(.*)$")


# ---------------------------------------------------------------------------
# 统一日志入口（供其他服务用中文写日志）
# ---------------------------------------------------------------------------

def _car_now_str() -> str:
    return format_car_time(now_car())


def _log_path() -> str:
    return os.path.join(get_state_dir(), "app.log")


def _rotate_locked(path: str) -> None:
    """日志超限时截断：保留后约 1MB，并从完整行开始。"""
    try:
        with open(path, "rb") as f:
            f.seek(-_MAX_LOG_BYTES // 2, os.SEEK_END)
            tail = f.read()
        nl = tail.find(b"\n")
        tail = tail[nl + 1:] if nl != -1 else tail
        with open(path, "wb") as f:
            f.write(tail)
    except OSError as e:
        logger.warning("日志截断失败：%s", e)


def log(level, msg) -> None:
    """写一条中文日志。level ∈ {debug, info, warning, error}，不抛异常。"""
    level = (level or "info").lower()
    if level not in ("debug", "info", "warning", "error"):
        level = "info"
    line = "{0} [{1}] {2}\n".format(_car_now_str(), level, msg)
    path = _log_path()
    with _LOG_LOCK:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path) and os.path.getsize(path) > _MAX_LOG_BYTES:
                _rotate_locked(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            logger.warning("写系统日志失败：%s", e)


def log_debug(msg) -> None:
    log("debug", msg)


def log_info(msg) -> None:
    log("info", msg)


def log_warning(msg) -> None:
    log("warning", msg)


def log_error(msg) -> None:
    log("error", msg)


# ---------------------------------------------------------------------------
# 模式（present / edit）
# ---------------------------------------------------------------------------

def _mode_path() -> str:
    return os.path.join(get_state_dir(), "mode.txt")


def _read_mode() -> str:
    """读 mode.txt；缺失或非法时默认为 present（开机即呈现模式）。"""
    try:
        with open(_mode_path(), encoding="utf-8") as f:
            mode = f.read().strip().lower()
            if mode in _VALID_MODES:
                return mode
    except OSError:
        pass
    return "present"


def _write_mode(mode: str) -> None:
    if mode not in _VALID_MODES:
        raise ValueError("模式必须是 present 或 edit")
    path = _mode_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(mode + "\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 硬件状态
# ---------------------------------------------------------------------------

def _read_temp_c():
    """CPU 温度（℃）。读不到返回 None。"""
    zones = []
    try:
        for name in sorted(os.listdir("/sys/class/thermal")):
            if name.startswith("thermal_zone"):
                zones.append(os.path.join("/sys/class/thermal", name, "temp"))
    except OSError:
        pass
    zones = zones or ["/sys/class/thermal/thermal_zone0/temp"]
    for zone in zones:
        try:
            with open(zone, encoding="utf-8") as f:
                return round(int(f.read().strip()) / 1000.0, 1)
        except (OSError, ValueError):
            continue
    return None


def _read_throttled() -> bool:
    """是否正在降频 / 欠压。无 vcgencmd 时返回 False（优雅降级）。"""
    try:
        r = subprocess.run(["vcgencmd", "get_throttled"],
                           capture_output=True, text=True, timeout=5,
                           check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    if r.returncode != 0:
        return False
    m = re.search(r"throttled=0x([0-9a-fA-F]+)", r.stdout or "")
    if not m:
        return False
    # bit0 欠压中 / bit1 限频中 / bit2 降频中
    return bool(int(m.group(1), 16) & 0x7)


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------

def _image_path(which: str) -> str:
    """镜像文件路径。which ∈ {"cam", "lightshow"}。"""
    name = "usb_cam.img" if which == "cam" else "usb_lightshow.img"
    base = os.environ.get("TESLAUSB_IMAGES_DIR") or os.path.join(_REPO_ROOT, "images")
    return os.path.join(base, name)


def _gb(num_bytes) -> float:
    return round(num_bytes / (1024 ** 3), 1)


def _usage_for(mount_point: str, image_path: str):
    """(total_gb, used_gb)。

    分区已挂载（edit 模式）时用真实文件系统用量；否则（present 模式，
    分区正呈现给特斯拉）用镜像文件估算：total 取镜像虚拟大小，used 取
    实际占用块（稀疏文件感知）。
    """
    if os.path.isdir(mount_point):
        try:
            u = shutil.disk_usage(mount_point)
            return _gb(u.total), _gb(u.used)
        except OSError:
            pass
    if os.path.isfile(image_path):
        try:
            st = os.stat(image_path)
            return _gb(st.st_size), _gb(st.st_blocks * 512)
        except OSError:
            pass
    return 0.0, 0.0


# ---------------------------------------------------------------------------
# 接口实现
# ---------------------------------------------------------------------------

def get_status() -> dict:
    """系统状态：{mode, temp_c, throttled, wifi: {ssid, signal}, version}。"""
    # 延迟导入：wifi_service 顶层导入本模块的 log，顶层互引会循环
    from . import wifi_service

    try:
        ws = wifi_service.get_wifi_status()
        wifi = {"ssid": ws.get("ssid"), "signal": ws.get("signal", 0)}
    except Exception as e:  # noqa: BLE001 - 状态页不因 WiFi 异常而崩
        logger.warning("读取 WiFi 状态失败：%s", e)
        wifi = {"ssid": None, "signal": 0}
    return {
        "mode": _read_mode(),
        "temp_c": _read_temp_c(),
        "throttled": _read_throttled(),
        "wifi": wifi,
        "version": VERSION,
    }


def get_storage() -> dict:
    """存储用量：{teslacam: {total_gb, used_gb}, lightshow: {...}}。"""
    mnt = get_mount_dir()
    cam_total, cam_used = _usage_for(os.path.join(mnt, "part1"),
                                     _image_path("cam"))
    ls_total, ls_used = _usage_for(os.path.join(mnt, "part2"),
                                   _image_path("lightshow"))
    return {
        "teslacam": {"total_gb": cam_total, "used_gb": cam_used},
        "lightshow": {"total_gb": ls_total, "used_gb": ls_used},
    }


def switch_mode(target) -> dict:
    """切换 USB 模式：present（呈现给特斯拉）/ edit（本地可读写）。

    调用方（Web 蓝图）负责二次确认；服务层只执行：调用同目录脚本，
    失败抛中文 RuntimeError，成功返回 {"mode": target, "ok": True}。
    """
    if target not in _VALID_MODES:
        raise ValueError("目标模式必须是 present 或 edit")
    script = os.path.join(
        _SCRIPTS_DIR,
        "present_usb.sh" if target == "present" else "edit_usb.sh")
    if not os.path.isfile(script):
        raise RuntimeError("模式切换脚本不存在：{0}".format(script))
    log_info("开始切换 USB 模式 → {0}…".format(target))
    try:
        r = subprocess.run(["bash", script], capture_output=True, text=True,
                           timeout=180, check=False)
    except subprocess.TimeoutExpired:
        raise RuntimeError("模式切换超时（180 秒），请检查 USB 与磁盘状态")
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "未知错误").strip().splitlines()[-5:]
        raise RuntimeError("模式切换失败：" + "；".join(tail))
    _write_mode(target)  # 脚本本身也会写；此处兜底保证一致
    log_info("USB 模式已切换为 {0}".format(target))
    return {"mode": target, "ok": True}


def get_logs(level=None) -> list:
    """读系统日志：[{time, level, msg}]，level 可按 info/warning/error/debug 筛选。"""
    entries = []
    try:
        with open(_log_path(), encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    for line in lines:
        line = line.rstrip("\n")
        m = _LOG_LINE_RE.match(line)
        if m:
            entries.append({"time": m.group(1), "level": m.group(2).lower(),
                            "msg": m.group(3)})
        elif line.strip():
            entries.append({"time": "", "level": "info", "msg": line.strip()})
    if level and level != "all":
        entries = [e for e in entries if e["level"] == level]
    return entries


def download_logs() -> str:
    """打包系统日志为 zip，返回 zip 文件路径。

    与 Web 蓝图 ``settings.logs_download`` 的 ``send_file(path)`` 用法、
    以及演示 mock（返回路径）保持一致：zip 落到 state 目录
    ``logs_export.zip``（重复导出直接覆盖）。
    """
    try:
        with open(_log_path(), encoding="utf-8") as f:
            log_text = f.read()
    except OSError:
        log_text = ""
    out_path = os.path.join(get_state_dir(), "logs_export.zip")
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("app.log", log_text)
        z.writestr("导出说明.txt",
                   "TeslaUSB-CN 系统日志\n导出时间（车机时间）：{0}\n版本：{1}\n"
                   .format(_car_now_str(), VERSION))
    return out_path


def clear_logs() -> bool:
    """清空系统日志，返回 True；失败抛中文 RuntimeError。"""
    path = _log_path()
    with _LOG_LOCK:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("")
        except OSError as e:
            raise RuntimeError("清空日志失败：{0}".format(e))
    return True
