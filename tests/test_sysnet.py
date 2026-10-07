"""C6（系统/网络/脚本）单测。

覆盖：
- wifi 在无 nmcli 时的优雅降级（不抛异常、返回中文提示的空结果）
- mode.txt 读写往返、get_status 形状
- storage 计算（tmp 路径：挂载目录用量 + 镜像文件估算）
- logs 写入 / 筛选 / 打包下载 / 清空
- shell 脚本 bash -n 语法检查 + 无遥测残留
- 与 web/blueprints/_services.py 的接口契约一致性

运行：cd ~/workspace/teslausb-cn && python3 -m pytest tests/test_sysnet.py -v
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.services import system_service, wifi_service  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = ["present_usb.sh", "edit_usb.sh", "ap_control.sh", "boot_present.sh"]


# ---------------------------------------------------------------------------
# 环境隔离
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """隔离状态/挂载/镜像目录到 tmp；强制无 nmcli（降级路径确定性测试）。"""
    monkeypatch.setenv("TESLAUSB_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TESLAUSB_MNT_DIR", str(tmp_path / "mnt"))
    monkeypatch.setenv("TESLAUSB_IMAGES_DIR", str(tmp_path / "images"))
    # nmcli 缺席：验证优雅降级（与本机是否真有 nmcli 无关）
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    return tmp_path


# ---------------------------------------------------------------------------
# 接口契约（与 web/blueprints/_services.py REQUIRED 对齐）
# ---------------------------------------------------------------------------

def test_interface_contract():
    for fn in ("get_wifi_status", "scan_networks", "connect", "forget",
               "saved_networks", "set_ap_mode", "get_ap_config",
               "set_ap_config"):
        assert callable(getattr(wifi_service, fn)), fn
    for fn in ("get_status", "get_storage", "switch_mode",
               "get_logs", "download_logs", "clear_logs"):
        assert callable(getattr(system_service, fn)), fn


# ---------------------------------------------------------------------------
# WiFi：无 nmcli 降级
# ---------------------------------------------------------------------------

def test_wifi_degrades_without_nmcli(isolated):
    st = wifi_service.get_wifi_status()
    assert st["ssid"] is None
    assert st["signal"] == 0
    assert st["mode"] == "client"
    assert "nmcli" in st.get("note", "")  # 中文提示

    assert wifi_service.scan_networks() == []
    assert wifi_service.saved_networks() == []

    r = wifi_service.connect("家里WiFi", "password123")
    assert r["success"] is False
    assert "nmcli" in r["message"]

    r = wifi_service.forget("家里WiFi")
    assert r["success"] is False
    assert "nmcli" in r["message"]


def test_wifi_connect_validation(isolated):
    # 参数校验先于 nmcli 检查：无 nmcli 环境下也应返回中文错误而非抛异常
    for bad_ssid in ("", "   ", "x" * 33):
        r = wifi_service.connect(bad_ssid, "password123")
        assert r["success"] is False and "SSID" in r["message"]
    r = wifi_service.connect("家里WiFi", "short")
    assert r["success"] is False and "8-63" in r["message"]


def test_wifi_scan_parser_handles_colon_in_ssid():
    # SSID 本身含冒号时切分不能错位
    middle, tails = wifi_service._split_middle_first("My:SSID:72:WPA2", 2)
    assert middle == "My:SSID"
    assert tails == ["72", "WPA2"]
    head, middle, tails = wifi_service._split_first_rest("yes:My:SSID:72", 1)
    assert (head, middle, tails) == ("yes", "My:SSID", ["72"])
    assert wifi_service._split_middle_first("abc", 2) is None


def test_ap_config_roundtrip(isolated):
    r = wifi_service.set_ap_config({"ssid": "测试热点", "password": "12345678"})
    assert r["success"] is True
    cfg = wifi_service.get_ap_config()
    assert cfg["ssid"] == "测试热点"
    assert cfg["password"] == "12345678"
    # 落盘文件存在且为合法 JSON
    with open(os.path.join(isolated, "home", "state", "ap_config.json"),
              encoding="utf-8") as f:
        assert json.load(f)["ssid"] == "测试热点"


def test_ap_config_validation(isolated):
    r = wifi_service.set_ap_config({"ssid": "", "password": "12345678"})
    assert r["success"] is False
    r = wifi_service.set_ap_config({"ssid": "x" * 33, "password": "12345678"})
    assert r["success"] is False
    r = wifi_service.set_ap_config({"ssid": "测试热点", "password": "short"})
    assert r["success"] is False and "8" in r["message"]


def test_set_ap_mode_no_script_graceful(isolated, monkeypatch):
    # 脚本缺失时不抛异常
    monkeypatch.setattr(wifi_service, "_SCRIPTS_DIR", str(isolated / "noscript"))
    r = wifi_service.set_ap_mode(True)
    assert r["mode"] == "ap" and r["ok"] is False
    assert "AP 控制脚本" in r["message"]


# ---------------------------------------------------------------------------
# system：mode / status / storage
# ---------------------------------------------------------------------------

def test_mode_read_write_roundtrip(isolated):
    assert system_service._read_mode() == "present"  # 缺失默认 present
    system_service._write_mode("edit")
    assert system_service._read_mode() == "edit"
    system_service._write_mode("present")
    assert system_service._read_mode() == "present"
    with pytest.raises(ValueError):
        system_service._write_mode("bogus")
    # 非法内容回退 present
    with open(os.path.join(isolated, "home", "state", "mode.txt"), "w") as f:
        f.write("weird\n")
    assert system_service._read_mode() == "present"


def test_get_status_shape(isolated):
    st = system_service.get_status()
    assert st["mode"] in ("present", "edit")
    assert st["version"] == system_service.VERSION
    assert isinstance(st["throttled"], bool)
    assert st["temp_c"] is None or isinstance(st["temp_c"], float)
    assert st["wifi"]["ssid"] is None  # 无 nmcli 降级
    assert st["wifi"]["signal"] == 0


def test_storage_tmp_paths(isolated):
    # edit 模式挂载目录：用真实文件系统用量
    part1 = isolated / "mnt" / "part1"
    part1.mkdir(parents=True)
    (part1 / "clip.mp4").write_bytes(b"\x00" * 1024 * 1024)
    s = system_service.get_storage()
    assert s["teslacam"]["total_gb"] > 0
    assert s["teslacam"]["used_gb"] >= 0
    # part2 未挂载、镜像不存在 → 0
    assert s["lightshow"] == {"total_gb": 0.0, "used_gb": 0.0}

    # present 模式：用镜像文件估算（稀疏文件：虚拟 100MB，实际块很少）
    images = isolated / "images"
    images.mkdir()
    img = images / "usb_lightshow.img"
    with open(img, "wb") as f:
        f.truncate(100 * 1024 * 1024)
    s = system_service.get_storage()
    assert s["lightshow"]["total_gb"] == pytest.approx(0.1, abs=0.02)
    assert s["lightshow"]["used_gb"] <= s["lightshow"]["total_gb"]


def test_switch_mode_validation():
    with pytest.raises(ValueError):
        system_service.switch_mode("bogus")


def test_switch_mode_missing_script(isolated, monkeypatch):
    monkeypatch.setattr(system_service, "_SCRIPTS_DIR",
                        str(isolated / "noscript"))
    with pytest.raises(RuntimeError, match="脚本不存在"):
        system_service.switch_mode("present")


# ---------------------------------------------------------------------------
# 日志：写入 / 筛选 / 打包下载 / 清空
# ---------------------------------------------------------------------------

def test_logs_write_filter_download_clear(isolated):
    system_service.log("info", "测试信息一")
    system_service.log("warning", "测试警告一")
    system_service.log("error", "测试错误一")

    entries = system_service.get_logs()
    assert len(entries) == 3
    assert all(re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}", e["time"])
               for e in entries)  # 车机时区时间戳（tzutil），非裸 fromtimestamp
    assert [e["level"] for e in entries] == ["info", "warning", "error"]
    assert "测试信息一" in entries[0]["msg"]

    assert len(system_service.get_logs("error")) == 1
    assert len(system_service.get_logs("all")) == 3
    assert len(system_service.get_logs("debug")) == 0

    zip_path = system_service.download_logs()
    assert isinstance(zip_path, str) and os.path.isfile(zip_path)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        assert "app.log" in names and "导出说明.txt" in names
        assert "测试警告一" in z.read("app.log").decode("utf-8")

    assert system_service.clear_logs() is True
    assert system_service.get_logs() == []


def test_log_helpers_and_bad_level(isolated):
    system_service.log_info("普通信息")
    system_service.log_warning("注意")
    system_service.log_error("出错了")
    system_service.log_debug("调试")
    system_service.log("weird-level", "非法级别回退为 info")
    levels = [e["level"] for e in system_service.get_logs()]
    assert levels == ["info", "warning", "error", "debug", "info"]


def test_get_logs_missing_file(isolated):
    assert system_service.get_logs() == []
    zip_path = system_service.download_logs()  # 无日志文件也不抛异常
    assert isinstance(zip_path, str) and os.path.isfile(zip_path)


# ---------------------------------------------------------------------------
# Shell 脚本：语法检查 + 无遥测残留 + 关键流程存在
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script", SCRIPTS)
def test_shell_syntax(script):
    path = os.path.join(REPO_ROOT, "scripts", script)
    assert os.path.isfile(path), "脚本缺失：" + script
    assert os.access(path, os.X_OK), "脚本无执行权限：" + script
    r = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
    assert r.returncode == 0, "bash -n 失败：{0}\n{1}".format(script, r.stderr)


@pytest.mark.parametrize("script", SCRIPTS)
def test_shell_chinese_and_safety(script):
    text = open(os.path.join(REPO_ROOT, "scripts", script),
                encoding="utf-8").read()
    assert "set -euo pipefail" in text
    # 精简要求：present/edit 不得含 analytics / 遥测上报残留（只查代码行，注释说明除外）
    code = "\n".join(l for l in text.splitlines()
                     if not l.lstrip().startswith("#")).lower()
    assert "analytics" not in code, script
    assert "telemetry" not in code, script


def test_present_edit_core_flow():
    present = open(os.path.join(REPO_ROOT, "scripts", "present_usb.sh"),
                   encoding="utf-8").read()
    assert "g_mass_storage" in present
    assert "mode.txt" in present and '"present"' in present
    edit = open(os.path.join(REPO_ROOT, "scripts", "edit_usb.sh"),
                encoding="utf-8").read()
    assert "rmmod g_mass_storage" in edit
    assert "mount" in edit and '"edit"' in edit and "mode.txt" in edit
    boot = open(os.path.join(REPO_ROOT, "scripts", "boot_present.sh"),
                encoding="utf-8").read()
    assert "present_usb.sh" in boot and "udc" in boot.lower()


def test_ap_control_status_json():
    r = subprocess.run(["bash", os.path.join(REPO_ROOT, "scripts",
                                             "ap_control.sh"), "status"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    st = json.loads(r.stdout)
    assert st["ap_active"] is False
    assert st["force_mode"] == "auto"
    assert st["ssid"] == "TeslaUSB-CN"
