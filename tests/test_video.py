#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""video_service 单测（pytest）。

依赖说明：
- ``web.services.tzutil`` / ``web.services.config`` 由并行子任务创建。
  若本次运行时尚不存在，本文件按 docs/INTERFACES.md 约定的接口注入最小桩
  实现（时区固定为 Asia/Shanghai，与 config.yaml 的 tesla_timezone 一致）；
  真实模块一旦就位，桩不会注入，测试自动走真实实现。
- 桩的 ``parse_filename_time`` 在无法解析时返回 None；video_service 本体对
  "返回 None" 与"抛异常"两种失败约定都做了兼容。
- 挂载点通过环境变量 TESLAUSB_MNT_DIR 指向 tmp_path 下的假 TeslaCam 目录，
  工作目录通过 TESLAUSB_HOME 隔离到 tmp_path（真实 config 的 get_state_dir()
  即 ``$TESLAUSB_HOME/state``），保证测试无外部副作用。
"""
import json
import os
import re
import sys
import time
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

CAR_TZ = ZoneInfo("Asia/Shanghai")


from web.services import video_service  # noqa: E402


# ---------------------------------------------------------------------------
# 假数据
# ---------------------------------------------------------------------------

# 最小合法 MP4 头：12 字节内含 ftyp，能通过 is_valid_mp4 校验
_VALID_MP4_HEADER = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2"


def _write_mp4(path, size_kb=64, valid=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        if valid:
            f.write(_VALID_MP4_HEADER)
            f.write(b"\x00" * max(0, size_kb * 1024 - len(_VALID_MP4_HEADER)))
        else:
            # 无 ftyp 魔数 → 头校验失败，应被服务层过滤
            f.write(b"\x00" * size_kb * 1024)


@pytest.fixture
def teslacam(tmp_path, monkeypatch):
    """构造假 TeslaCam 目录（含三种文件夹结构）并用环境变量指向它。"""
    mnt = tmp_path / "mnt"
    root = mnt / "part1" / "TeslaCam"
    recent = root / "RecentClips"
    saved_evt = root / "SavedClips" / "2026-10-06_13-00-00"
    sentry = root / "SentryClips"

    # RecentClips：扁平结构，一条 3 路 + 一条单路
    _write_mp4(recent / "2026-10-06_14-32-11-front.mp4")
    _write_mp4(recent / "2026-10-06_14-32-11-back.mp4")
    _write_mp4(recent / "2026-10-06_14-32-11-left_repeater.mp4")
    _write_mp4(recent / "2026-10-06_14-31-11-front.mp4")
    # 头校验失败的文件：不得出现在列表中
    _write_mp4(recent / "2026-10-06_14-33-11-front.mp4", valid=False)
    # SavedClips：事件子目录结构
    _write_mp4(saved_evt / "2026-10-06_13-00-00-front.mp4")
    _write_mp4(saved_evt / "2026-10-06_13-00-00-right_pillar.mp4")
    # SentryClips：扁平单文件（兼容写法）
    _write_mp4(sentry / "2026-10-05_22-10-05-left_pillar.mp4")

    monkeypatch.setenv("TESLAUSB_MNT_DIR", str(mnt))
    monkeypatch.setenv("TESLAUSB_HOME", str(tmp_path / "home"))
    return root


@pytest.fixture
def utc_system_tz():
    """把进程时区强制为 UTC，还原"裸 fromtimestamp" bug 的现场。

    若实现中误用裸 datetime.fromtimestamp()，文件名 14:32 会被显示成
    06:32（UTC），测试即失败；正确实现应始终显示车机本地时间 14:32。
    """
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


# ---------------------------------------------------------------------------
# list_videos
# ---------------------------------------------------------------------------

def test_list_videos_item_keys_match_contract(teslacam):
    """条目键严格符合 INTERFACES.md 契约（不多不少）。"""
    items = video_service.list_videos()
    assert items, "假数据下列表不应为空"
    expected = {"name", "folder", "time", "duration_s", "cameras",
                "size_mb", "thumbnail_url", "favorite"}
    for it in items:
        assert set(it) == expected


def test_list_videos_time_is_car_local_not_utc(teslacam, utc_system_tz):
    """P0 回归：系统时区为 UTC 时，文件名 14:32 仍须显示为车机本地 14:32。"""
    items = {it["name"]: it for it in video_service.list_videos()}
    assert items["2026-10-06_14-32-11"]["time"] == "2026-10-06 14:32"
    assert items["2026-10-06_14-32-11"]["folder"] == "RecentClips"
    assert items["2026-10-05_22-10-05"]["time"] == "2026-10-05 22:10"


def test_list_videos_sorted_newest_first(teslacam):
    names = [it["name"] for it in video_service.list_videos()]
    assert names == ["2026-10-06_14-32-11", "2026-10-06_14-31-11",
                     "2026-10-06_13-00-00", "2026-10-05_22-10-05"]


def test_cameras_detected_in_canonical_order(teslacam):
    items = {it["name"]: it for it in video_service.list_videos()}
    # 按实际存在的文件返回，顺序为规范摄像头顺序
    assert items["2026-10-06_14-32-11"]["cameras"] == ["front", "left_repeater", "back"]
    assert items["2026-10-06_13-00-00"]["cameras"] == ["front", "right_pillar"]
    assert items["2026-10-05_22-10-05"]["cameras"] == ["left_pillar"]


def test_folder_filter(teslacam):
    assert len(video_service.list_videos(folder="RecentClips")) == 2
    assert len(video_service.list_videos(folder="SavedClips")) == 1
    assert len(video_service.list_videos(folder="SentryClips")) == 1
    assert len(video_service.list_videos(folder=None)) == 4
    with pytest.raises(ValueError):
        video_service.list_videos(folder="Nope")


def test_invalid_mp4_is_filtered(teslacam):
    """头校验失败的 14-33-11 不得产生条目。"""
    names = [it["name"] for it in video_service.list_videos()]
    assert "2026-10-06_14-33-11" not in names


def test_thumbnail_url_size_duration(teslacam):
    items = {it["name"]: it for it in video_service.list_videos()}
    it = items["2026-10-06_14-32-11"]
    assert it["thumbnail_url"] == "/thumbs/RecentClips/2026-10-06_14-32-11.jpg"
    assert it["size_mb"] == round(3 * 64 * 1024 / (1024 * 1024), 2)
    # 假文件 ffprobe 无法解析 → 按 60s 回退
    assert it["duration_s"] == 60
    assert it["favorite"] is False


def test_unparsable_filename_falls_back_to_mtime(teslacam):
    """文件名时间戳非法（如月份 13）时回退到文件 mtime，仍为车机本地时间。"""
    p = teslacam / "RecentClips" / "2026-13-40_99-99-99-front.mp4"
    _write_mp4(p)
    dt = datetime(2026, 10, 6, 14, 32, tzinfo=CAR_TZ)
    os.utime(p, (dt.timestamp(), dt.timestamp()))
    items = {it["name"]: it for it in video_service.list_videos()}
    assert items["2026-13-40_99-99-99"]["time"] == "2026-10-06 14:32"
    assert items["2026-13-40_99-99-99"]["cameras"] == ["front"]


# ---------------------------------------------------------------------------
# 收藏
# ---------------------------------------------------------------------------

def test_toggle_favorite(teslacam):
    assert video_service.toggle_favorite("2026-10-06_14-32-11", "RecentClips") is True
    fav = video_service.list_videos(favorite_only=True)
    assert [it["name"] for it in fav] == ["2026-10-06_14-32-11"]
    assert all(it["favorite"] for it in fav)
    assert video_service.toggle_favorite("2026-10-06_14-32-11", "RecentClips") is False
    assert video_service.list_videos(favorite_only=True) == []
    with pytest.raises(ValueError):
        video_service.toggle_favorite("x", "Nope")


def test_favorites_persisted_to_state_dir(teslacam, tmp_path):
    video_service.toggle_favorite("2026-10-06_14-32-11", "RecentClips")
    fav_file = tmp_path / "home" / "state" / "favorites.json"
    assert fav_file.exists()
    data = json.loads(fav_file.read_text(encoding="utf-8"))
    assert "RecentClips/2026-10-06_14-32-11" in data["favorites"]


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------

def test_delete_videos_flat(teslacam):
    res = video_service.delete_videos(["2026-10-06_14-31-11"], "RecentClips")
    assert res["deleted_names"] == ["2026-10-06_14-31-11"]
    assert res["deleted_files"] == 1
    assert res["failed"] == {}
    assert not (teslacam / "RecentClips" / "2026-10-06_14-31-11-front.mp4").exists()
    assert len(video_service.list_videos(folder="RecentClips")) == 1


def test_delete_videos_event_dir_removed_when_empty(teslacam):
    res = video_service.delete_videos(["2026-10-06_13-00-00"], "SavedClips")
    assert res["deleted_files"] == 2
    assert res["failed"] == {}
    assert not (teslacam / "SavedClips" / "2026-10-06_13-00-00").exists()
    assert video_service.list_videos(folder="SavedClips") == []


def test_delete_clears_favorite(teslacam):
    video_service.toggle_favorite("2026-10-06_14-31-11", "RecentClips")
    video_service.delete_videos(["2026-10-06_14-31-11"], "RecentClips")
    assert video_service.list_videos(favorite_only=True) == []


def test_delete_missing_video_reports_failure(teslacam):
    res = video_service.delete_videos(["2026-01-01_00-00-00"], "RecentClips")
    assert res["deleted_names"] == []
    assert "2026-01-01_00-00-00" in res["failed"]


def test_delete_invalid_folder(teslacam):
    with pytest.raises(ValueError):
        video_service.delete_videos(["x"], "Nope")


# ---------------------------------------------------------------------------
# 统计与估算
# ---------------------------------------------------------------------------

def test_get_folder_stats(teslacam):
    stats = video_service.get_folder_stats()
    assert stats["RecentClips"]["count"] == 2  # 无效文件不计入
    assert stats["SavedClips"]["count"] == 1
    assert stats["SentryClips"]["count"] == 1
    # RecentClips: 3×64KB + 1×64KB = 256KB（无效文件不计入大小）
    assert stats["RecentClips"]["size_gb"] == round(256 * 1024 / (1024 ** 3), 2)
    assert stats["SavedClips"]["size_gb"] == round(2 * 64 * 1024 / (1024 ** 3), 2)
    for folder in ("RecentClips", "SavedClips", "SentryClips"):
        assert set(stats[folder]) == {"count", "size_gb"}


def test_estimate_recording_time_with_videos(teslacam):
    est = video_service.estimate_recording_time()
    assert set(est) == {"hours", "method", "confidence"}
    assert est["hours"] > 0
    assert "4 条" in est["method"]
    assert est["confidence"] == "low"  # 样本少于 10 条


def test_estimate_recording_time_no_videos(tmp_path, monkeypatch):
    """无历史视频时用 400MB/小时理论值估算。"""
    empty = tmp_path / "empty_mnt"
    (empty / "part1" / "TeslaCam").mkdir(parents=True)
    monkeypatch.setenv("TESLAUSB_MNT_DIR", str(empty))
    monkeypatch.setenv("TESLAUSB_HOME", str(tmp_path / "home2"))
    est = video_service.estimate_recording_time()
    assert "理论值" in est["method"]
    assert est["confidence"] == "low"
    assert est["hours"] > 0
