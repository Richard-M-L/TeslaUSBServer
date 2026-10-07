"""Phase 1 C1 基础层单测：config / tzutil / jobs / sei_parser。

纯 Python，无硬件依赖；在无 protobuf 的 VM 上验证 sei_parser 的降级路径。
运行：cd <仓库根> && python -m pytest tests/test_foundation.py -v
"""

import logging
import os
import sys
import threading
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from web.services import config as config_mod  # noqa: E402
from web.services import jobs  # noqa: E402
from web.services import tzutil  # noqa: E402
from web.services.sei_parser import (  # noqa: E402
    SeiMessage,
    _decode_sei_nal,
    _get_sei_metadata_class,
    extract_sei_messages,
    parse_video_sei,
)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_load_config_defaults(self):
        cfg = config_mod.load_config()
        assert isinstance(cfg, dict)
        # 仓库根 config.yaml 自带车机时区配置
        assert cfg.get("tesla_timezone") == "Asia/Shanghai"

    def test_tesla_timezone(self):
        tz = config_mod.get_tesla_timezone()
        assert str(tz.key) == "Asia/Shanghai"

    def test_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TESLAUSB_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("TESLAUSB_MNT_DIR", str(tmp_path / "mnt"))
        assert config_mod.get_home_dir() == str(tmp_path / "home")
        assert config_mod.get_mount_dir() == str(tmp_path / "mnt")
        assert config_mod.get_teslacam_root() == str(
            tmp_path / "mnt" / "part1" / "TeslaCam")
        assert config_mod.get_lightshow_root() == str(tmp_path / "mnt" / "part2")

    def test_state_dir_created(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TESLAUSB_HOME", str(tmp_path / "home2"))
        state = config_mod.get_state_dir()
        assert state == str(tmp_path / "home2" / "state")
        assert os.path.isdir(state)

    def test_get_config_value(self):
        assert config_mod.get_config_value(
            "cleanup", "recentclips_retention_days") == 7
        assert config_mod.get_config_value("backup", "enabled") is False
        assert config_mod.get_config_value("not", "exist", default="d") == "d"
        # 中间层不是 dict 时也返回 default
        assert config_mod.get_config_value(
            "tesla_timezone", "x", default="d") == "d"
        # 无 key 时返回整个配置
        assert isinstance(config_mod.get_config_value(), dict)

    def test_config_file_override(self, tmp_path, monkeypatch):
        p = tmp_path / "custom.yaml"
        p.write_text('tesla_timezone: "UTC"\n', encoding="utf-8")
        monkeypatch.setenv("TESLAUSB_CONFIG", str(p))
        config_mod.reload_config()
        try:
            assert config_mod.load_config()["tesla_timezone"] == "UTC"
            assert str(config_mod.get_tesla_timezone().key) == "UTC"
        finally:
            monkeypatch.undo()
            config_mod.reload_config()

    def test_missing_config_file_no_raise(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TESLAUSB_CONFIG", str(tmp_path / "nope.yaml"))
        config_mod.reload_config()
        try:
            assert config_mod.load_config() == {}
        finally:
            monkeypatch.undo()
            config_mod.reload_config()


# ---------------------------------------------------------------------------
# tzutil（时区 P0：无裸 fromtimestamp）
# ---------------------------------------------------------------------------

class TestTzutil:
    def test_car_tz_default(self):
        assert str(tzutil.car_tz().key) == "Asia/Shanghai"

    def test_parse_filename_time(self):
        dt = tzutil.parse_filename_time("2026-10-07_08-30-00-front.mp4")
        assert dt is not None
        assert (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second) == \
            (2026, 10, 7, 8, 30, 0)
        # aware，且为车机时区（语义：车机本地时间，不是 UTC）
        assert dt.tzinfo is not None
        assert str(dt.tzinfo.key) == "Asia/Shanghai"
        assert dt.utcoffset().total_seconds() == 8 * 3600

    def test_parse_filename_time_with_path(self):
        dt = tzutil.parse_filename_time(
            "/mnt/teslausb/part1/TeslaCam/RecentClips/2026-10-07_08-30-00-front.mp4")
        assert dt is not None and dt.hour == 8 and dt.minute == 30

    def test_parse_filename_time_invalid(self):
        assert tzutil.parse_filename_time("no-timestamp.mp4") is None
        assert tzutil.parse_filename_time("") is None
        # 非法日期（13 月）→ None，不抛异常
        assert tzutil.parse_filename_time("2026-13-99_99-99-99-x.mp4") is None

    def test_format_car_time(self):
        dt = tzutil.parse_filename_time("2026-10-07_08-30-00-front.mp4")
        assert tzutil.format_car_time(dt) == "2026-10-07 08:30"

    def test_format_car_time_converts_utc(self):
        from datetime import datetime, timezone
        # 00:30 UTC == 08:30 北京时间
        dt = datetime(2026, 10, 7, 0, 30, tzinfo=timezone.utc)
        assert tzutil.format_car_time(dt) == "2026-10-07 08:30"

    def test_epoch_roundtrip(self):
        ts = 1760000000.0
        car = tzutil.epoch_to_car(ts)
        assert car.tzinfo is not None
        assert str(car.tzinfo.key) == "Asia/Shanghai"
        # 往返：车机时间 → epoch 应该还原
        assert tzutil.car_to_epoch(car) == pytest.approx(ts)

    def test_now_car(self):
        now = tzutil.now_car()
        assert now.tzinfo is not None
        assert str(now.tzinfo.key) == "Asia/Shanghai"

    def test_no_bare_fromtimestamp_in_tzutil(self):
        # P0 回归：tzutil 内不允许出现无 tz 参数的 fromtimestamp 调用
        # （先剔除 docstring 与行注释，避免注释中的示例文字误报）
        src = open(tzutil.__file__, encoding="utf-8").read()
        import re
        src = re.sub(r'"""[\s\S]*?"""', "", src)
        src = re.sub(r"#.*", "", src)
        for m in re.finditer(r"fromtimestamp\(([^)]*)\)", src):
            assert "tz" in m.group(1), f"发现裸 fromtimestamp：{m.group(0)}"


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------

def _wait_state(job_id, states, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = jobs.get_job_progress(job_id)
        if p["state"] in states:
            return p
        time.sleep(0.02)
    return jobs.get_job_progress(job_id)


class TestJobs:
    def test_start_and_done(self):
        seen = []

        def target(progress):
            progress(50, "处理中")
            seen.append(jobs.get_job_progress)
            progress(100, "全部完成")

        jid = jobs.start_job("单测任务", target)
        p = _wait_state(jid, {"done"})
        assert p["state"] == "done"
        assert p["percent"] == 100
        assert p["message"] == "全部完成"
        assert p["name"] == "单测任务"

    def test_progress_visible_while_running(self):
        started = threading.Event()
        finish = threading.Event()

        def target(progress):
            progress(30, "进行到三成")
            started.set()
            finish.wait(5)

        jid = jobs.start_job("进度任务", target)
        assert started.wait(5)
        p = jobs.get_job_progress(jid)
        assert p["state"] == "running"
        assert p["percent"] == 30
        assert p["message"] == "进行到三成"
        finish.set()
        _wait_state(jid, {"done"})

    def test_exception_becomes_failed(self):
        def target(progress):
            progress(10, "开始")
            raise RuntimeError("磁盘满了")

        jid = jobs.start_job("异常任务", target)
        p = _wait_state(jid, {"failed"})
        assert p["state"] == "failed"
        assert "失败" in p["message"]
        assert "磁盘满了" in p["message"]

    def test_cancel(self):
        def target(progress):
            i = 0
            while True:
                i += 1
                progress(i % 100, f"第{i}轮")

        jid = jobs.start_job("可取消任务", target)
        time.sleep(0.2)
        assert jobs.cancel_job(jid) is True
        p = _wait_state(jid, {"cancelled"})
        assert p["state"] == "cancelled"
        assert "取消" in p["message"]

    def test_cancel_unknown_or_finished(self):
        assert jobs.cancel_job("不存在的任务-0-abc123") is False

        done_evt = threading.Event()

        def target(progress):
            done_evt.set()

        jid = jobs.start_job("快任务", target)
        _wait_state(jid, {"done"})
        assert jobs.cancel_job(jid) is False

    def test_unknown_job_progress(self):
        p = jobs.get_job_progress("不存在的任务-0-abc123")
        assert p["state"] == "unknown"
        assert "不存在" in p["message"]

    def test_target_args_kwargs(self):
        got = {}

        def target(progress, a, b=0):
            got["a"] = a
            got["b"] = b
            progress(100, "收尾")

        jid = jobs.start_job("参数任务", target, 1, b=2)
        _wait_state(jid, {"done"})
        assert got == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# sei_parser（本 VM 无 protobuf：验证降级路径 + 纯逻辑）
# ---------------------------------------------------------------------------

def _msg(**kw):
    base = dict(
        frame_index=0, timestamp_ms=0.0,
        latitude_deg=0.0, longitude_deg=0.0, heading_deg=0.0,
        vehicle_speed_mps=0.0,
        linear_acceleration_x=0.0, linear_acceleration_y=0.0,
        linear_acceleration_z=0.0,
        steering_wheel_angle=0.0, accelerator_pedal_position=0.0,
        brake_applied=False, gear_state="PARK", autopilot_state="NONE",
        blinker_on_left=False, blinker_on_right=False,
        frame_seq_no=0, video_path="x.mp4",
    )
    base.update(kw)
    return SeiMessage(**base)


class TestSeiParser:
    def test_protobuf_unavailable_degrades_gracefully(self, caplog):
        # 本 VM 未安装 protobuf：应返回 None 且不抛异常
        with caplog.at_level(logging.WARNING):
            cls = _get_sei_metadata_class()
        assert cls is None

    def test_parse_degraded_returns_empty_no_raise(self, tmp_path, caplog):
        fake = tmp_path / "2026-10-07_08-30-00-front.mp4"
        fake.write_bytes(b"\x00" * 64)  # 非法 MP4 也无妨：降级路径不读文件
        with caplog.at_level(logging.WARNING):
            assert parse_video_sei(str(fake)) == []
        assert any("protobuf" in r.message for r in caplog.records)

    def test_extract_degraded_nonexistent_file_no_raise(self):
        # 降级路径：文件不存在也不抛异常（README 约定）
        assert list(extract_sei_messages("/不存在/的/文件.mp4")) == []

    def test_decode_sei_nal_too_short(self):
        assert _decode_sei_nal(b"\x00\x01") is None

    def test_has_movement_by_speed(self):
        assert _msg(vehicle_speed_mps=10.0).has_movement is True
        assert _msg(vehicle_speed_mps=0.4).has_movement is False  # 低于蠕行阈值

    def test_has_movement_by_gear(self):
        assert _msg(gear_state="DRIVE").has_movement is True
        assert _msg(gear_state="REVERSE").has_movement is True
        assert _msg(gear_state="PARK").has_movement is False

    def test_has_movement_by_autopilot(self):
        assert _msg(autopilot_state="TACC").has_movement is True
        assert _msg(autopilot_state="AUTOSTEER").has_movement is True
        assert _msg(autopilot_state="NONE").has_movement is False

    def test_has_coordinates(self):
        assert _msg().has_coordinates is False  # 国行：无 GPS 遥测
        assert _msg(latitude_deg=31.2, longitude_deg=121.4).has_coordinates is True

    def test_speed_kph(self):
        assert _msg(vehicle_speed_mps=10.0).speed_kph == pytest.approx(36.0)
