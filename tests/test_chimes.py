"""锁车提示音 / 定时计划 / 随机分组 / 灯光秀单测。

说明：
- 使用真实的 web.services.config / tzutil 模块，路径通过环境变量覆盖：
  ``TESLAUSB_HOME``（state 目录的父级）、``TESLAUSB_MNT_DIR``（挂载根，
  LightShow 分区为其下 part2），每个测试相互隔离。
- 随机相关测试用 ``random.seed()`` 保证确定性（被测代码不重置随机种子）。
"""
import os
import random
import shutil
import sys
import wave
from datetime import datetime

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from web.services import chime_scheduler, lightshow_service, lock_chime_service  # noqa: F401,E402
from web.services.chime_scheduler import ChimeGroupManager, ChimeScheduler  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def lightshow_root(tmp_path, monkeypatch):
    mnt = tmp_path / "mnt"
    root = mnt / "part2"
    root.mkdir(parents=True)
    monkeypatch.setenv("TESLAUSB_MNT_DIR", str(mnt))
    return root


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TESLAUSB_HOME", str(home))
    return home / "state"


@pytest.fixture()
def scheduler(state_dir):  # noqa: ARG001
    return ChimeScheduler()


@pytest.fixture()
def groups(state_dir):  # noqa: ARG001
    return ChimeGroupManager()


class FakeUpload:
    """最小上传对象：有 filename 与 save()，模拟 Flask FileStorage。"""

    def __init__(self, filename, data: bytes):
        self.filename = filename
        self._data = data

    def save(self, dest):
        with open(dest, "wb") as fh:
            fh.write(self._data)


def make_wav(path, seconds=1.0, framerate=44100, nchannels=1, sampwidth=2):
    frames = int(seconds * framerate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(nchannels)
        w.setsampwidth(sampwidth)
        w.setframerate(framerate)
        w.writeframes(b"\x00" * frames * nchannels * sampwidth)
    with open(path, "rb") as fh:
        return fh.read()


def upload_wav(name="a.wav", **kwargs):
    data = make_wav("/tmp/_t.wav", **kwargs)
    return lock_chime_service.upload_chime(FakeUpload(name, data))


# ---------------------------------------------------------------------------
# lock_chime_service
# ---------------------------------------------------------------------------

def test_upload_set_active_delete_roundtrip(lightshow_root, tmp_path):
    ok, msg = upload_wav("a.wav")
    assert ok, msg
    ok, msg = upload_wav("b.wav", seconds=0.5)
    assert ok, msg

    chimes = lock_chime_service.list_chimes()
    assert [c["name"] for c in chimes] == ["a.wav", "b.wav"]
    assert chimes[0]["duration_s"] == pytest.approx(1.0, abs=0.05)
    assert all(c["is_active"] is False for c in chimes)

    ok, msg = lock_chime_service.set_active("a.wav")
    assert ok, msg
    active_path = lightshow_root / "LockChime.wav"
    assert active_path.is_file() and not active_path.is_symlink()

    chimes = {c["name"]: c for c in lock_chime_service.list_chimes()}
    assert chimes["a.wav"]["is_active"] is True
    assert chimes["b.wav"]["is_active"] is False

    ok, msg = lock_chime_service.set_active("b.wav")
    assert ok, msg
    chimes = {c["name"]: c for c in lock_chime_service.list_chimes()}
    assert chimes["a.wav"]["is_active"] is False
    assert chimes["b.wav"]["is_active"] is True

    # 删除当前生效的：曲库条目与 LockChime.wav 一并清除
    ok, msg = lock_chime_service.delete_chime("b.wav")
    assert ok, msg
    assert not active_path.exists()
    chimes = lock_chime_service.list_chimes()
    assert [c["name"] for c in chimes] == ["a.wav"]
    assert chimes[0]["is_active"] is False

    ok, _ = lock_chime_service.delete_chime("a.wav")
    assert ok
    assert lock_chime_service.list_chimes() == []


def test_upload_rejects_invalid(lightshow_root):  # noqa: ARG001
    ok, msg = lock_chime_service.upload_chime(FakeUpload("x.mp3", b"data"))
    assert not ok and ".wav" in msg

    ok, msg = lock_chime_service.upload_chime(
        FakeUpload("big.wav", b"RIFF" + b"\x00" * (2 * 1024 * 1024)))
    assert not ok and "1MB" in msg

    bad8 = make_wav("/tmp/_t8.wav", sampwidth=1)
    ok, msg = lock_chime_service.upload_chime(FakeUpload("bad8.wav", bad8))
    assert not ok and "16-bit" in msg

    ok, _ = lock_chime_service.set_active("not-exist.wav")
    assert not ok
    ok, _ = lock_chime_service.delete_chime("not-exist.wav")
    assert not ok


def test_upload_without_ffmpeg_does_not_block(lightshow_root, monkeypatch):  # noqa: ARG001
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    ok, msg = lock_chime_service.upload_chime(
        FakeUpload("a.wav", make_wav("/tmp/_t.wav")), normalize_volume=True)
    assert ok, msg
    assert [c["name"] for c in lock_chime_service.list_chimes()] == ["a.wav"]


def test_normalize_with_ffmpeg(lightshow_root):  # noqa: ARG001
    if shutil.which("ffmpeg") is None:
        pytest.skip("VM 上无 ffmpeg")
    stereo48 = make_wav("/tmp/_t48.wav", framerate=48000, nchannels=2)
    ok, msg = lock_chime_service.upload_chime(
        FakeUpload("s.wav", stereo48), normalize_volume=True)
    assert ok, msg
    path = os.path.join(str(lightshow_root), "Chimes", "s.wav")
    with wave.open(path, "rb") as w:
        assert w.getframerate() == 44100
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2

    ok, msg = lock_chime_service.upload_chime(
        FakeUpload("s2.wav", stereo48), normalize_volume=False)
    assert ok, msg
    path2 = os.path.join(str(lightshow_root), "Chimes", "s2.wav")
    with wave.open(path2, "rb") as w:
        assert w.getframerate() == 48000  # 未归一化，保持原样


# ---------------------------------------------------------------------------
# ChimeScheduler：四类计划
# ---------------------------------------------------------------------------

def test_weekly_schedule_hit_and_miss(scheduler):
    ok, msg, sid = scheduler.create_schedule({
        "name": "周一早", "type": "weekly", "chime": "a.wav",
        "time": "08:00", "days": ["周一"], "enabled": True,
    })
    assert ok, msg
    # 2026-02-16 是周一
    assert scheduler.get_active_chime(datetime(2026, 2, 16, 9, 0)) == "a.wav"
    assert scheduler.get_active_chime(datetime(2026, 2, 16, 7, 59)) is None
    # 2026-02-18 是周三：今天无计划，昨天（周二）也无计划 -> None
    # （注：周二会命中"昨天回退"逻辑，故不用周二断言无命中）
    assert scheduler.get_active_chime(datetime(2026, 2, 18, 9, 0)) is None

    # 同日同时刻冲突
    ok, msg, _ = scheduler.create_schedule({
        "name": "冲突", "type": "weekly", "chime": "b.wav",
        "time": "08:00", "days": ["Monday"],
    })
    assert not ok and "冲突" in msg


def test_holiday_beats_weekly(scheduler):
    scheduler.create_schedule({"name": "周二", "type": "weekly", "chime": "b.wav",
                               "time": "08:00", "days": ["Tuesday"]})
    scheduler.create_schedule({"name": "春节", "type": "holiday", "chime": "c.wav",
                               "time": "07:00", "holiday": "春节"})
    # 2026-02-17 是周二，同时是春节：节日计划优先
    assert scheduler.get_active_chime(datetime(2026, 2, 17, 9, 0)) == "c.wav"
    # 时刻未到：两个计划都没过
    assert scheduler.get_active_chime(datetime(2026, 2, 17, 6, 0)) is None
    # 次日回退：昨天（春节/周二）的计划仍然生效，节日优先
    assert scheduler.get_active_chime(datetime(2026, 2, 18, 9, 0)) == "c.wav"


def test_date_schedule_and_cleanup(scheduler):
    ok, msg, sid = scheduler.create_schedule({
        "name": "三八", "type": "date", "chime": "d.wav",
        "time": "10:00", "date": "03-08",
    })
    assert ok, msg
    # 2026-03-08 是周日
    assert scheduler.get_active_chime(datetime(2026, 3, 8, 11, 0)) == "d.wav"
    assert scheduler.get_active_chime(datetime(2026, 3, 8, 9, 59)) is None

    # 记录执行后，已过期的 date 计划可被清理
    assert scheduler.record_execution(sid, datetime(2026, 3, 8, 10, 5))
    assert scheduler.cleanup_expired_date_schedules(datetime(2026, 3, 9, 0, 0)) == 1
    assert scheduler.get_schedule(sid) is None


def test_schedule_update_delete(scheduler):
    ok, _, sid = scheduler.create_schedule({
        "name": "旧", "type": "weekly", "chime": "a.wav",
        "time": "08:00", "days": ["Monday"],
    })
    assert ok
    ok, msg = scheduler.update_schedule(sid, time="09:30", name="新名")
    assert ok, msg
    s = scheduler.get_schedule(sid)
    assert s["time"] == "09:30" and s["name"] == "新名"
    # 更新后按新时间命中
    assert scheduler.get_active_chime(datetime(2026, 2, 16, 9, 0)) is None
    assert scheduler.get_active_chime(datetime(2026, 2, 16, 10, 0)) == "a.wav"

    ok, _ = scheduler.delete_schedule(sid)
    assert ok and scheduler.get_schedule(sid) is None
    ok, _ = scheduler.delete_schedule(sid)
    assert not ok


def test_recurring_on_boot_executes_once(scheduler):
    ok, msg, sid = scheduler.create_schedule({
        "name": "开机随机", "type": "recurring", "interval": "on_boot",
    })
    assert ok, msg
    assert scheduler.get_schedule(sid)["chime_filename"] == "RANDOM"

    # 同一时间只能有一个启用的循环计划
    ok, msg, _ = scheduler.create_schedule({
        "name": "第二个", "type": "recurring", "interval": "1hour",
    })
    assert not ok

    should, chime, _ = scheduler.should_execute_schedule(sid)
    assert should and chime == "RANDOM"
    assert scheduler.record_execution(sid)
    should, _, _ = scheduler.should_execute_schedule(sid)
    assert not should  # 本次启动已执行过


def test_random_chime_schedule_avoids_current(scheduler, lightshow_root):  # noqa: ARG001
    upload_wav("a.wav")
    upload_wav("b.wav")
    lock_chime_service.set_active("a.wav")
    scheduler.create_schedule({"name": "随", "type": "weekly", "chime": "RANDOM",
                               "time": "08:00", "days": ["Monday"]})
    random.seed(7)
    picked = scheduler.get_active_chime(datetime(2026, 2, 16, 9, 0))
    assert picked == "b.wav"  # 避开当前生效的 a.wav，只剩 b.wav


def test_holiday_table_2026(state_dir):  # noqa: ARG001
    table = {h["name"]: (h["month"], h["day"])
             for h in chime_scheduler.get_holidays_with_dates(2026)}
    assert table["春节"] == (2, 17)
    assert table["元旦"] == (1, 1)
    assert table["国庆"] == (10, 1)


def test_default_state_path_uses_config(state_dir):
    s = ChimeScheduler()
    ok, _, _ = s.create_schedule({"name": "x", "type": "weekly", "chime": "a.wav",
                                  "time": "08:00", "days": ["Monday"]})
    assert ok
    assert os.path.isfile(os.path.join(str(state_dir), "schedules.json"))


# ---------------------------------------------------------------------------
# ChimeGroupManager：分组与随机模式
# ---------------------------------------------------------------------------

def _make_pool(groups):
    ok, _, gid = groups.create_group("池")
    assert ok
    for name in ("a.wav", "b.wav", "c.wav"):
        assert groups.add_to_group(gid, name)[0]
    return gid


def test_group_crud(groups):
    ok, msg, gid = groups.create_group("早高峰")
    assert ok, msg
    ok, _, _ = groups.create_group("早高峰")
    assert not ok  # 重名

    assert groups.add_to_group(gid, "a.wav")[0]
    assert not groups.add_to_group(gid, "a.wav")[0]  # 重复加入
    assert groups.add_to_group(gid, "b.wav")[0]
    assert not groups.add_to_group("nope", "a.wav")[0]

    listed = groups.list_groups()
    assert len(listed) == 1 and listed[0]["chime_count"] == 2
    assert listed[0]["chimes"] == ["a.wav", "b.wav"]

    assert groups.remove_from_group(gid, "a.wav")[0]
    assert not groups.remove_from_group(gid, "a.wav")[0]
    assert groups.get_group(gid)["chime_count"] == 1

    assert groups.delete_group(gid)[0]
    assert groups.list_groups() == []
    assert not groups.delete_group(gid)[0]


def test_random_mode_requires_source(groups):
    gid = _make_pool(groups)
    ok, msg = groups.set_random_mode(True)
    assert not ok and "随机源" in msg
    ok, msg = groups.set_random_source("nope")
    assert not ok
    ok, msg = groups.set_random_source(gid)
    assert ok, msg
    assert groups.get_random_source() == gid
    ok, msg = groups.set_random_mode(True)
    assert ok, msg
    assert groups.get_random_mode() is True


def test_random_selection_deterministic(groups):
    gid = _make_pool(groups)
    groups.set_random_source(gid)
    groups.set_random_mode(True)

    random.seed(123)
    p1 = groups.select_random_chime()
    random.seed(123)
    p2 = groups.select_random_chime()
    assert p1 == p2 and p1 in ("a.wav", "b.wav", "c.wav")

    avoided = groups.select_random_chime(avoid_chime=p1)
    assert avoided != p1 and avoided in ("a.wav", "b.wav", "c.wav")

    groups.set_random_mode(False)
    assert groups.get_random_mode() is False
    assert groups.select_random_chime() is None


def test_boot_random_applies_to_active(groups, lightshow_root):  # noqa: ARG001
    gid = _make_pool(groups)
    groups.set_random_source(gid)
    groups.set_random_mode(True)

    upload_wav("a.wav", seconds=1.0)
    upload_wav("b.wav", seconds=0.7)
    upload_wav("c.wav", seconds=1.3)
    lock_chime_service.set_active("a.wav")

    random.seed(5)
    ok, msg = groups.apply_boot_random_chime()
    assert ok, msg
    active = {c["name"] for c in lock_chime_service.list_chimes() if c["is_active"]}
    assert len(active) == 1
    picked = next(iter(active))
    assert picked in ("a.wav", "b.wav", "c.wav") and picked != "a.wav"  # 避开开机前当前


def test_delete_random_source_group_blocked(groups):
    gid = _make_pool(groups)
    groups.set_random_source(gid)
    groups.set_random_mode(True)
    ok, msg = groups.delete_group(gid)
    assert not ok and "随机" in msg
    groups.set_random_mode(False)
    assert groups.delete_group(gid)[0]


# ---------------------------------------------------------------------------
# lightshow_service
# ---------------------------------------------------------------------------

def test_lightshow_upload_list_delete(lightshow_root):
    ok, msg = lightshow_service.upload_show(
        FakeUpload("mymix.fseq", b"FSEQDATA"),
        FakeUpload("mymix.mp3", b"MP3DATA"))
    assert ok, msg
    show_dir = lightshow_root / "mymix"
    assert (show_dir / "mymix.fseq").is_file()
    assert (show_dir / "mymix.mp3").is_file()

    shows = lightshow_service.list_shows()
    assert shows == [{"name": "mymix", "has_fseq": True, "has_mp3": True}]

    ok, msg = lightshow_service.upload_show(
        FakeUpload("other.fseq", b"x"), FakeUpload("mymix.mp3", b"y"))
    assert not ok and "配对" in msg

    ok, msg = lightshow_service.delete_show("mymix")
    assert ok, msg
    assert not show_dir.exists()
    assert lightshow_service.list_shows() == []
    ok, _ = lightshow_service.delete_show("mymix")
    assert not ok
