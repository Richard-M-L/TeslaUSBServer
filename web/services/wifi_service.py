"""WiFi 管理服务（NetworkManager / nmcli 封装，slim 版）。

接口（docs/INTERFACES.md）::

    get_wifi_status() -> {ssid, signal, mode: client/ap}
    scan_networks()   -> [{ssid, signal, secured}]
    connect(ssid, password) / forget(ssid) / saved_networks()
    set_ap_mode(enabled) / get_ap_config() / set_ap_config({ssid, password})

移植自上游 fork ``scripts/web/services/wifi_service.py``（978 行），仅保留
Phase 1 需要的核心能力：nmcli 扫描 / 连接 / 已保存网络管理 / AP 切换。

降级约定：本机没有 ``nmcli``（如开发机 / Docker）时不抛异常，返回带中文
提示的空结果；AP 切换通过 ``scripts/ap_control.sh`` 完成。

纯标准库 + ``web.services.config``，不依赖 Flask。
"""

import json
import logging
import os
import shutil
import subprocess
import threading
import time

from .config import get_state_dir
from .system_service import log  # 统一中文日志入口（system_service 不在顶层反向导入本模块，无循环）

logger = logging.getLogger(__name__)

# 仓库根（本文件位于 <repo>/web/services/）
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
_AP_CONFIG_FILE = "ap_config.json"

_NO_NMCLI_NOTE = "本机未找到 nmcli（NetworkManager 未安装），WiFi 管理功能不可用"

# 串行化用户发起的连接尝试：两次同时点击“连接”会争抢无线网卡
_CONNECT_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _has_nmcli() -> bool:
    """本机是否有可用的 nmcli。"""
    return shutil.which("nmcli") is not None


def _run(cmd, timeout=10):
    """运行命令，返回 (returncode, stdout, stderr)。

    命令缺失 / 超时 / 其他异常一律转为 (-1, "", 中文说明），不抛异常。
    """
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, check=False)
        return r.returncode, r.stdout or "", r.stderr or ""
    except FileNotFoundError:
        return -1, "", "命令不存在：{0}".format(cmd[0] if cmd else "?")
    except subprocess.TimeoutExpired:
        return -1, "", "命令执行超时（{0} 秒）".format(timeout)
    except Exception as e:  # noqa: BLE001 - 降级路径不抛异常
        return -1, "", str(e)


def _split_first_rest(line, n_tail):
    """切分 ``HEAD:MIDDLE...:TAIL`` 型行，MIDDLE 可含冒号。

    返回 (head, middle, tails)；字段不足返回 None。
    例：``yes:My:SSID:72`` + n_tail=1 → ``("yes", "My:SSID", ["72"])``。
    """
    parts = line.split(":")
    if len(parts) < 1 + n_tail:
        return None
    head = parts[0].strip()
    tails = [p.strip() for p in parts[-n_tail:]] if n_tail else []
    end = len(parts) - n_tail if n_tail else len(parts)
    middle = ":".join(parts[1:end]).strip()
    return head, middle, tails


def _split_middle_first(line, n_tail):
    """切分 ``MIDDLE...:TAIL`` 型行，MIDDLE（SSID/连接名）可含冒号。

    返回 (middle, tails)；字段不足返回 None。
    例：``My:SSID:72:WPA2`` + n_tail=2 → ``("My:SSID", ["72", "WPA2"])``。
    """
    parts = line.split(":")
    if len(parts) < 1 + n_tail:
        return None
    tails = [p.strip() for p in parts[-n_tail:]] if n_tail else []
    end = len(parts) - n_tail if n_tail else len(parts)
    middle = ":".join(parts[:end]).strip()
    return middle, tails


def _ap_control(*args):
    """调用 scripts/ap_control.sh，返回 (rc, stdout, stderr)。"""
    script = os.path.join(_SCRIPTS_DIR, "ap_control.sh")
    if not os.path.isfile(script):
        return -1, "", "AP 控制脚本不存在：{0}".format(script)
    return _run(["bash", script] + list(args), timeout=15)


def _ap_active() -> bool:
    """热点当前是否在运行（解析 ap_control.sh status 的 JSON）。"""
    rc, out, _ = _ap_control("status")
    if rc != 0:
        return False
    try:
        return bool(json.loads(out).get("ap_active"))
    except (ValueError, AttributeError):
        return False


def _ap_config_path() -> str:
    return os.path.join(get_state_dir(), _AP_CONFIG_FILE)


def _read_ap_config() -> dict:
    try:
        with open(_ap_config_path(), encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_ap_config(cfg: dict) -> None:
    path = _ap_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# nmcli 查询
# ---------------------------------------------------------------------------

def _current_connection():
    """当前 WiFi 连接：(ssid, signal)。未连接返回 (None, 0)。"""
    rc, out, _ = _run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"],
                      timeout=8)
    if rc != 0:
        return None, 0
    for line in out.splitlines():
        parsed = _split_first_rest(line, 1)
        if parsed and parsed[0] == "yes":
            _, ssid, (signal_s,) = parsed
            try:
                signal = int(signal_s)
            except ValueError:
                signal = 0
            return (ssid or None), signal
    return None, 0


def _find_connection_name(ssid):
    """按 SSID 找 NetworkManager 连接名，找不到返回 None。"""
    rc, out, _ = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
                      timeout=8)
    if rc != 0:
        return None
    for line in out.splitlines():
        parsed = _split_middle_first(line, 1)
        if parsed and "wireless" in parsed[1][0].lower():
            name = parsed[0]
            rc2, out2, _ = _run(
                ["nmcli", "-t", "-f", "802-11-wireless.ssid",
                 "connection", "show", name],
                timeout=8)
            if rc2 == 0:
                conn_ssid = out2.strip().split(":", 1)[-1].strip()
                if conn_ssid == ssid:
                    return name
    return None


def _active_connection_name():
    """wlan0 上当前激活的连接名，无则返回 None。"""
    rc, out, _ = _run(["nmcli", "-t", "-f", "NAME,DEVICE",
                       "connection", "show", "--active"], timeout=8)
    if rc != 0:
        return None
    for line in out.splitlines():
        parsed = _split_middle_first(line, 1)
        if parsed and parsed[1][0] == "wlan0":
            return parsed[0] or None
    return None


def _activate(name, timeout=30) -> bool:
    """激活一个已保存连接，成功返回 True。"""
    rc, _, _ = _run(["sudo", "-n", "nmcli", "connection", "up", name],
                    timeout=timeout)
    if rc != 0:
        return False
    time.sleep(2)
    ssid, _ = _current_connection()
    return ssid is not None


# ---------------------------------------------------------------------------
# 接口实现
# ---------------------------------------------------------------------------

def get_wifi_status() -> dict:
    """当前 WiFi 状态：{ssid, signal, mode: client/ap}。"""
    if not _has_nmcli():
        return {"ssid": None, "signal": 0, "mode": "client",
                "note": _NO_NMCLI_NOTE}
    ssid, signal = _current_connection()
    return {"ssid": ssid, "signal": signal,
            "mode": "ap" if _ap_active() else "client"}


def scan_networks() -> list:
    """扫描可见 WiFi，按信号强度降序：[{ssid, signal, secured}]。"""
    if not _has_nmcli():
        log("warning", "扫描 WiFi 需要 nmcli，当前环境不可用，已返回空列表")
        return []
    # 触发一次重新扫描（best effort，失败也不影响读取缓存结果）
    _run(["sudo", "-n", "nmcli", "dev", "wifi", "rescan"], timeout=10)
    time.sleep(1)
    rc, out, _ = _run(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY",
                       "dev", "wifi", "list"], timeout=10)
    if rc != 0:
        return []
    networks, seen = [], set()
    for line in out.splitlines():
        parsed = _split_middle_first(line, 2)
        if not parsed:
            continue
        ssid, (signal_s, security) = parsed
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        try:
            signal = int(signal_s)
        except ValueError:
            signal = 0
        networks.append({
            "ssid": ssid,
            "signal": signal,
            "secured": bool(security and security != "--"),
        })
    networks.sort(key=lambda n: n["signal"], reverse=True)
    return networks


def saved_networks() -> list:
    """已保存的 WiFi 网络，按优先级降序：[{name, ssid, signal, active, priority}]。"""
    if not _has_nmcli():
        return []
    rc, out, _ = _run(["nmcli", "-t", "-f", "NAME,TYPE,AUTOCONNECT-PRIORITY",
                       "connection", "show"], timeout=8)
    if rc != 0:
        return []
    conns = []
    for line in out.splitlines():
        parsed = _split_middle_first(line, 2)
        if parsed and "wireless" in parsed[1][0].lower():
            name, (_, priority_s) = parsed
            try:
                priority = int(priority_s) if priority_s else 0
            except ValueError:
                priority = 0
            conns.append({"name": name, "priority": priority})
    if not conns:
        return []

    # 可见网络的信号强度（用于展示）
    visible = {}
    rc, out, _ = _run(["nmcli", "-t", "-f", "SSID,SIGNAL", "dev", "wifi", "list"],
                      timeout=8)
    if rc == 0:
        for line in out.splitlines():
            parsed = _split_middle_first(line, 1)
            if parsed and parsed[0]:
                try:
                    sig = int(parsed[1][0])
                except ValueError:
                    sig = 0
                if parsed[0] not in visible or sig > visible[parsed[0]]:
                    visible[parsed[0]] = sig

    active_name = _active_connection_name()
    networks = []
    for conn in conns:
        rc, out, _ = _run(["nmcli", "-t", "-f", "802-11-wireless.ssid",
                           "connection", "show", conn["name"]], timeout=8)
        ssid = ""
        if rc == 0 and out.strip():
            ssid = out.strip().split(":", 1)[-1].strip()
        networks.append({
            "name": conn["name"],
            "ssid": ssid or conn["name"],
            "signal": visible.get(ssid, 0),
            "active": conn["name"] == active_name,
            "priority": conn["priority"],
        })
    networks.sort(key=lambda n: n["priority"], reverse=True)
    return networks


def connect(ssid, password="") -> dict:
    """连接到 WiFi。返回 {success, message, ...}，失败自动回滚旧连接。

    失败兜底顺序：回滚到之前的连接 → 还不行则开启热点（AP fallback），
    保证 Web 界面始终可访问。
    """
    ssid = (ssid or "").strip()
    password = password or ""
    if not ssid or len(ssid) > 32:
        return {"success": False, "message": "SSID 不能为空且不超过 32 个字符"}
    if password and (len(password) < 8 or len(password) > 63):
        return {"success": False,
                "message": "WiFi 密码需为 8-63 个字符（开放网络可留空）"}
    if not _has_nmcli():
        return {"success": False, "message": _NO_NMCLI_NOTE}
    if not _CONNECT_LOCK.acquire(blocking=False):
        return {"success": False, "in_progress": True,
                "message": "已有连接任务在进行中，请稍候再试"}
    try:
        return _connect_locked(ssid, password)
    finally:
        _CONNECT_LOCK.release()


def _connect_locked(ssid, password) -> dict:
    prev_ssid, _ = _current_connection()
    prev_name = _active_connection_name()
    existing = _find_connection_name(ssid)
    conn_name = existing or "WiFi-{0}".format(ssid)

    if existing:
        # 修改已保存连接的密码
        if password:
            cmd = ["sudo", "-n", "nmcli", "connection", "modify", conn_name,
                   "wifi.ssid", ssid,
                   "wifi-sec.key-mgmt", "wpa-psk",
                   "wifi-sec.psk", password]
        else:
            cmd = ["sudo", "-n", "nmcli", "connection", "modify", conn_name,
                   "wifi.ssid", ssid,
                   "wifi-sec.key-mgmt", "none"]
        rc, _, err = _run(cmd, timeout=10)
        if rc != 0:
            return {"success": False,
                    "message": "更新已保存网络失败：{0}".format(err or "未知错误")}
    else:
        # 新建连接并连接
        cmd = ["sudo", "-n", "nmcli", "device", "wifi", "connect", ssid,
               "name", conn_name]
        if password:
            cmd += ["password", password]
        rc, _, err = _run(cmd, timeout=30)
        if rc != 0 and "No network with SSID" in err:
            return {"success": False,
                    "message": "找不到网络「{0}」，请确认 SSID 正确".format(ssid)}
        # 其他错误先继续走“激活 + 验证”流程，nmcli 报错不一定代表失败

    # 激活并验证（NetworkManager 上报不稳定，以实际连接状态为准）
    _run(["sudo", "-n", "nmcli", "connection", "up", conn_name], timeout=30)
    ok = False
    for _ in range(6):
        time.sleep(2)
        cur_ssid, _ = _current_connection()
        if cur_ssid == ssid:
            ok = True
            break

    if ok:
        # 新连接提为最高优先级
        _run(["sudo", "-n", "nmcli", "connection", "modify", conn_name,
              "connection.autoconnect-priority", "100"], timeout=8)
        log("info", "WiFi 已连接到「{0}」".format(ssid))
        return {"success": True, "ssid": ssid,
                "message": "已连接到「{0}」".format(ssid)}

    # 失败：尝试回滚到之前的连接
    reverted = False
    if prev_name:
        reverted = _activate(prev_name, timeout=20)
    if reverted:
        log("warning", "连接「{0}」失败，已恢复之前的网络「{1}」".format(ssid, prev_ssid))
        return {"success": False, "action": "reverted",
                "message": "连接「{0}」失败，已恢复之前的网络「{1}」".format(ssid, prev_ssid)}

    # 回滚也失败：开启热点兜底，保证还能连上 Web
    _ap_control("force-on")
    log("error", "连接「{0}」失败且无法恢复旧网络，已开启热点供重新配置".format(ssid))
    return {"success": False, "action": "ap_started",
            "message": "连接「{0}」失败，已开启热点，请连接热点后重新配置".format(ssid)}


def forget(ssid) -> dict:
    """删除已保存的 WiFi 网络。若删的是当前连接，先切到备选网络。"""
    ssid = (ssid or "").strip()
    if not ssid:
        return {"success": False, "message": "SSID 不能为空"}
    if not _has_nmcli():
        return {"success": False, "message": _NO_NMCLI_NOTE}

    saved = saved_networks()
    if len(saved) <= 1:
        return {"success": False,
                "message": "不能删除唯一已保存的网络（至少保留一个）"}
    target = next((n for n in saved if n["ssid"] == ssid), None)
    if target is None:
        return {"success": False,
                "message": "未找到已保存的网络「{0}」".format(ssid)}

    if target.get("active"):
        switched = False
        for alt in saved:
            if alt["ssid"] != ssid and _activate(alt["name"], timeout=20):
                switched = True
                break
        if not switched:
            _ap_control("force-on")  # 兜底：开热点，避免失联

    rc, _, err = _run(["sudo", "-n", "nmcli", "connection", "delete",
                       target["name"]], timeout=10)
    if rc != 0:
        return {"success": False,
                "message": "删除失败：{0}".format((err or "未知错误").strip())}
    log("info", "已删除已保存的 WiFi 网络「{0}」".format(ssid))
    return {"success": True, "message": "已删除网络「{0}」".format(ssid)}


def set_ap_mode(enabled) -> dict:
    """开关热点（AP）模式：调用 scripts/ap_control.sh。"""
    enabled = bool(enabled)
    rc, _, err = _ap_control("force-on" if enabled else "force-off")
    mode = "ap" if enabled else "client"
    if rc != 0:
        msg = "热点模式切换失败：{0}".format(err or "AP 控制脚本异常")
        log("warning", msg)
        return {"mode": mode, "ok": False, "message": msg}
    log("info", "已{0}热点模式".format("开启" if enabled else "关闭"))
    return {"mode": mode, "ok": True,
            "message": "已{0}热点模式".format("开启" if enabled else "关闭")}


def get_ap_config() -> dict:
    """热点配置：{ssid, password}。"""
    cfg = _read_ap_config()
    return {"ssid": cfg.get("ssid") or "TeslaUSB-CN",
            "password": cfg.get("password") or ""}


def set_ap_config(cfg) -> dict:
    """更新热点配置并落盘；热点运行中时通知 ap_control.sh 重载生效。"""
    cfg = cfg or {}
    ssid = str(cfg.get("ssid", "")).strip()
    password = str(cfg.get("password", ""))
    if not ssid or len(ssid) > 32:
        return {"success": False, "message": "热点名称不能为空且不超过 32 个字符"}
    if password and len(password) < 8:
        return {"success": False, "message": "热点密码至少 8 个字符（开放热点可留空）"}
    _write_ap_config({"ssid": ssid, "password": password})
    if _ap_active():
        _ap_control("reload")  # best effort：让新配置生效
    log("info", "热点配置已更新（SSID：{0}）".format(ssid))
    return {"success": True, "message": "热点配置已保存"}
