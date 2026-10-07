"""备份与清理服务的单测。

测试策略：
- 直接使用真实的 ``web/services/config.py``、``tzutil.py``、``jobs.py``；
  路径通过环境变量 ``TESLAUSB_MNT_DIR`` / ``TESLAUSB_HOME`` 覆盖
  （tmp_path 隔离）；配置项覆盖通过 ``_cfg_overrides`` fixture
  （monkeypatch 服务模块内的 ``get_config_value``，自动还原）。
- rclone 二进制用 monkeypatch 伪造（当前 VM 无 rclone，正好覆盖
  "rclone 缺失时中文报错" 的分支）。
"""
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

# ---------------------------------------------------------------------------
# 使用真实共享模块；配置覆盖 fixture（见下方 _cfg_overrides）
# ---------------------------------------------------------------------------

from web.services import backup_service, cleanup_service, rclone_service  # noqa: E402
from web.services import config as _config_real  # noqa: E402


@pytest.fixture(autouse=True)
def _cfg_overrides(monkeypatch):
    """按测试覆盖 get_config_value（自动还原）。

    用法：在测试函数签名中加入 ``_cfg_overrides`` 参数，然后
    ``_cfg_overrides[("backup", "delete_after_backup")] = True``。
    未覆盖的 key 走真实 config.yaml。
    """
    overrides = {}
    real = _config_real.get_config_value

    def fake(*keys, default=None):
        if keys in overrides:
            return overrides[keys]
        return real(*keys, default=default)

    monkeypatch.setattr(backup_service, "get_config_value", fake)
    monkeypatch.setattr(cleanup_service, "get_config_value", fake)
    return overrides

from web.services import backup_service, cleanup_service, rclone_service  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def teslacam(tmp_path, monkeypatch):
    mnt = tmp_path / "mnt"
    root = mnt / "part1" / "TeslaCam"
    for f in ("RecentClips", "SavedClips", "SentryClips"):
        (root / f).mkdir(parents=True)
    monkeypatch.setenv("TESLAUSB_MNT_DIR", str(mnt))
    return root


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TESLAUSB_HOME", str(home))
    return home / "state"


def make_video(path, size_kb=10, age_seconds=0):
    path.write_bytes(b"\x00" * (size_kb * 1024))
    ts = time.time() - age_seconds
    os.utime(path, (ts, ts))
    return path


def wait_for_done(get_progress, job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = get_progress(job_id)
        if s["state"] in ("done", "failed", "cancelled"):
            return s
        time.sleep(0.05)
    raise TimeoutError("任务 %s 未在 %ss 内结束：%s" % (job_id, timeout, s))


DAY = 86400


# ---------------------------------------------------------------------------
# rclone_service：NAS 配置与密码加密
# ---------------------------------------------------------------------------

class TestRcloneService:
    def test_configure_nas_encrypts_password(self, state_dir):
        rclone_service.configure_nas({
            "protocol": "smb", "host": "192.168.1.10", "port": 445,
            "username": "lu", "password": "supersecret123",
        })
        raw = (state_dir / "nas_creds.enc").read_bytes()
        assert b"supersecret123" not in raw, "密码明文落盘！"
        assert b"192.168.1.10" not in raw, "配置应整体加密"

        cfg = rclone_service.get_nas_config()
        assert cfg["configured"] is True
        assert cfg["protocol"] == "smb"
        assert cfg["host"] == "192.168.1.10"
        assert cfg["port"] == 445
        assert cfg["username"] == "lu"
        assert cfg["has_password"] is True
        assert "password" not in cfg

        # 解密回环：密码原文可恢复
        creds = rclone_service._load_creds()
        assert creds["pass"] == "supersecret123"
        assert creds["type"] == "smb"

    def test_configure_nas_defaults_port(self, state_dir):
        rclone_service.configure_nas({
            "protocol": "sftp", "host": "nas.local", "username": "u",
            "password": "p",
        })
        assert rclone_service.get_nas_config()["port"] == 22

    def test_configure_nas_webdav_url(self, state_dir):
        rclone_service.configure_nas({
            "protocol": "webdav", "host": "192.168.1.20", "port": 5005,
            "username": "u", "password": "p",
        })
        creds = rclone_service._load_creds()
        assert creds["type"] == "webdav"
        assert creds["url"] == "http://192.168.1.20:5005/"

    def test_configure_nas_rejects_bad_input(self, state_dir):
        with pytest.raises(ValueError):
            rclone_service.configure_nas({"protocol": "s3", "host": "h"})
        with pytest.raises(ValueError):
            rclone_service.configure_nas(
                {"protocol": "smb", "host": "", "port": 445})
        with pytest.raises(ValueError):
            rclone_service.configure_nas(
                {"protocol": "smb", "host": "h", "port": 99999})
        with pytest.raises(ValueError):
            rclone_service.configure_nas(
                {"protocol": "smb", "host": "h", "port": "abc"})
        # rclone.conf 注入防护：值中含换行直接拒绝
        with pytest.raises(ValueError):
            rclone_service.configure_nas({
                "protocol": "smb", "host": "h\ntype = local",
                "username": "u", "password": "p"})

    def test_remove_nas_config(self, state_dir):
        rclone_service.configure_nas({
            "protocol": "nfs", "host": "nas.local", "username": "",
            "password": ""})
        assert rclone_service.get_nas_config()["configured"] is True
        rclone_service.remove_nas_config()
        assert rclone_service.get_nas_config()["configured"] is False

    def test_obfuscated_cipher_never_plaintext(self, state_dir):
        # cryptography 缺失时回退路径：混淆存储，可逆但非明文
        cipher = rclone_service._ObfuscatedCipher()
        blob = cipher.encrypt("密码123".encode("utf-8"))
        assert "密码123".encode("utf-8") not in blob
        assert cipher.decrypt(blob) == "密码123".encode("utf-8")


# ---------------------------------------------------------------------------
# backup_service：备份流程
# ---------------------------------------------------------------------------

class TestBackupService:
    def _enable_fake_rclone(self, monkeypatch):
        monkeypatch.setattr(rclone_service, "rclone_available",
                            lambda: True)

    def _fake_sync(self, monkeypatch, delay=0):
        def _sync(local_dir, remote_subpath, progress_cb=None,
                  total_bytes=0, base_bytes=0):
            if delay:
                time.sleep(delay)
            total = 0
            for name in os.listdir(local_dir):
                p = os.path.join(local_dir, name)
                if os.path.isfile(p):
                    total += os.path.getsize(p)
            if progress_cb:
                progress_cb(50, "fake sync")
            return total
        monkeypatch.setattr(backup_service, "_sync_one_folder", _sync)

    def test_rclone_missing_fails_in_chinese(self, teslacam, state_dir,
                                            monkeypatch):
        monkeypatch.setattr(rclone_service, "rclone_available",
                            lambda: False)
        make_video(teslacam / "RecentClips" / "a.mp4", age_seconds=2 * DAY)
        job_id = backup_service.start_backup()  # 不抛异常
        assert isinstance(job_id, str)
        s = wait_for_done(backup_service.get_backup_progress, job_id)
        assert s["state"] == "failed"
        assert "rclone" in s["message"]

    def test_backup_success_writes_history(self, teslacam, state_dir,
                                           monkeypatch):
        self._enable_fake_rclone(monkeypatch)
        self._fake_sync(monkeypatch)
        rclone_service.configure_nas({
            "protocol": "smb", "host": "192.168.1.10", "port": 445,
            "username": "u", "password": "p"})
        make_video(teslacam / "RecentClips" / "a.mp4", size_kb=100,
                   age_seconds=2 * DAY)
        make_video(teslacam / "SentryClips" / "b.mp4", size_kb=200,
                   age_seconds=3 * DAY)

        job_id = backup_service.start_backup()
        s = wait_for_done(backup_service.get_backup_progress, job_id)
        assert s["state"] == "done", s
        assert s["percent"] == 100
        assert s["total"] == 2
        assert s["current"] == 2

        history = backup_service.get_backup_history()
        assert len(history) == 1
        entry = history[0]
        assert entry["files"] == 2
        assert entry["status"] == "成功"
        assert entry["size_gb"] == round(300 * 1024 / (1024 ** 3), 2)
        # 车机时区格式 YYYY-MM-DD HH:MM
        datetime.strptime(entry["time"], "%Y-%m-%d %H:%M")

        # delete_after_backup=false：本地文件保留
        assert (teslacam / "RecentClips" / "a.mp4").exists()

    def test_no_nas_config_fails_in_chinese(self, teslacam, state_dir,
                                            monkeypatch):
        self._enable_fake_rclone(monkeypatch)
        make_video(teslacam / "RecentClips" / "a.mp4", age_seconds=2 * DAY)
        job_id = backup_service.start_backup()
        s = wait_for_done(backup_service.get_backup_progress, job_id)
        assert s["state"] == "failed"
        assert "NAS" in s["message"]

    def test_delete_after_backup_double_check(self, teslacam, state_dir,
                                              monkeypatch, _cfg_overrides):
        """二次校验：备份过程中关闭开关 → 不删除本地文件。"""
        self._enable_fake_rclone(monkeypatch)
        self._fake_sync(monkeypatch, delay=0.6)
        rclone_service.configure_nas({
            "protocol": "smb", "host": "192.168.1.10",
            "username": "u", "password": "p"})
        _cfg_overrides[("backup", "delete_after_backup")] = True
        v = make_video(teslacam / "RecentClips" / "a.mp4", age_seconds=2 * DAY)

        job_id = backup_service.start_backup()
        # 备份尚未完成时关闭开关
        _cfg_overrides[("backup", "delete_after_backup")] = False
        s = wait_for_done(backup_service.get_backup_progress, job_id)
        assert s["state"] == "done", s
        assert v.exists(), "开关已关闭，本地文件不应被删除"

    def test_delete_after_backup_enabled(self, teslacam, state_dir,
                                         monkeypatch, _cfg_overrides):
        """开关全程开启 → 备份成功后删除本地文件。"""
        self._enable_fake_rclone(monkeypatch)
        self._fake_sync(monkeypatch)
        rclone_service.configure_nas({
            "protocol": "smb", "host": "192.168.1.10",
            "username": "u", "password": "p"})
        _cfg_overrides[("backup", "delete_after_backup")] = True
        v = make_video(teslacam / "SavedClips" / "keep.mp4",
                       age_seconds=10 * DAY)

        job_id = backup_service.start_backup()
        s = wait_for_done(backup_service.get_backup_progress, job_id)
        assert s["state"] == "done", s
        assert not v.exists(), "开关开启时本地文件应被删除"

    def test_backup_cancel_via_progress(self, teslacam, state_dir,
                                          monkeypatch):
        """取消经由 progress 抛内部异常传播（镜像真 jobs.py 的协作式取消）；
        取消后写一条"已取消"历史。"""
        self._enable_fake_rclone(monkeypatch)
        rclone_service.configure_nas({
            "protocol": "smb", "host": "192.168.1.10",
            "username": "u", "password": "p"})

        def slow_sync(local_dir, remote_subpath, progress_cb=None,
                      total_bytes=0, base_bytes=0):
            for pct in range(0, 100, 10):
                time.sleep(0.05)
                if progress_cb:
                    progress_cb(pct, "fake")  # 取消后此处抛取消异常
            return 0

        monkeypatch.setattr(backup_service, "_sync_one_folder", slow_sync)
        make_video(teslacam / "RecentClips" / "a.mp4", age_seconds=2 * DAY)
        make_video(teslacam / "SentryClips" / "b.mp4", age_seconds=2 * DAY)

        job_id = backup_service.start_backup()
        time.sleep(0.2)
        assert backup_service.cancel_job(job_id) is True
        s = wait_for_done(backup_service.get_backup_progress, job_id)
        assert s["state"] == "cancelled", s
        history = backup_service.get_backup_history()
        assert history and history[0]["status"] == "已取消"

    def test_unknown_job_progress(self, state_dir):
        s = backup_service.get_backup_progress("job-not-exist")
        assert s["state"] in ("failed", "unknown")


# ---------------------------------------------------------------------------
# cleanup_service：保留策略
# ---------------------------------------------------------------------------

class TestCleanupService:
    def test_retention_rules(self, teslacam, state_dir):
        # RecentClips 保留 7 天：10 天前删，1 天前留
        old = make_video(teslacam / "RecentClips" / "old.mp4",
                         age_seconds=10 * DAY)
        new = make_video(teslacam / "RecentClips" / "new.mp4",
                         age_seconds=1 * DAY)
        # 1 小时保护：30 分钟前的文件永不删除
        fresh = make_video(teslacam / "RecentClips" / "fresh.mp4",
                           age_seconds=1800)
        # SavedClips retention=0：30 天前也不删
        saved = make_video(teslacam / "SavedClips" / "saved.mp4",
                           age_seconds=30 * DAY)
        # SentryClips 保留 30 天：40 天前删
        sentry_old = make_video(teslacam / "SentryClips" / "sentry.mp4",
                                age_seconds=40 * DAY)

        preview = cleanup_service.preview_cleanup()
        assert preview["files"] == 2  # old.mp4 + sentry.mp4
        assert preview["size_gb"] >= 0
        # 预览不删除任何文件
        for p in (old, new, fresh, saved, sentry_old):
            assert p.exists()

        job_id = cleanup_service.run_cleanup()
        s = wait_for_done(cleanup_service.get_cleanup_progress, job_id)
        assert s["state"] == "done", s
        assert s["total"] == 2
        assert not old.exists()
        assert not sentry_old.exists()
        assert new.exists()
        assert fresh.exists(), "1 小时内的视频永不删除"
        assert saved.exists(), "SavedClips retention=0 不清理"

    def test_savedclips_zero_means_never(self, teslacam, state_dir):
        v = make_video(teslacam / "SavedClips" / "ancient.mp4",
                       age_seconds=400 * DAY)
        assert cleanup_service.preview_cleanup()["files"] == 0
        job_id = cleanup_service.run_cleanup()
        wait_for_done(cleanup_service.get_cleanup_progress, job_id)
        assert v.exists()

    def test_protect_last_hour_explicit(self, teslacam, state_dir):
        v = make_video(teslacam / "RecentClips" / "just.mp4",
                       age_seconds=600)
        # 显式把保留天数设得很激进也删不掉 10 分钟前的文件
        cleanup_service.set_retention_config(
            {"recentclips_retention_days": 7})
        assert cleanup_service.preview_cleanup()["files"] == 0
        job_id = cleanup_service.run_cleanup()
        wait_for_done(cleanup_service.get_cleanup_progress, job_id)
        assert v.exists()

    def test_set_retention_config_validation(self, state_dir):
        with pytest.raises(ValueError):
            cleanup_service.set_retention_config(
                {"recentclips_retention_days": -1})
        with pytest.raises(ValueError):
            cleanup_service.set_retention_config(
                {"recentclips_retention_days": "很多"})
        with pytest.raises(ValueError):
            cleanup_service.set_retention_config({"no_such_key": 1})

        assert cleanup_service.set_retention_config(
            {"recentclips_retention_days": 3,
             "run_on_boot": True}) is True
        cfg = cleanup_service.get_retention_config()
        assert cfg["recentclips_retention_days"] == 3
        assert cfg["run_on_boot"] is True
        assert cfg["savedclips_retention_days"] == 0  # 默认值保留
        # 持久化到 state/（覆盖文件存在）
        assert (state_dir / "cleanup_retention.json").is_file()

    def test_cleanup_cancel(self, teslacam, state_dir, monkeypatch):
        for i in range(30):
            make_video(teslacam / "RecentClips" / ("f%02d.mp4" % i),
                       age_seconds=10 * DAY)

        real_remove = os.remove

        def slow_remove(p):
            time.sleep(0.05)
            real_remove(p)

        monkeypatch.setattr(os, "remove", slow_remove)
        job_id = cleanup_service.run_cleanup()
        time.sleep(0.3)
        assert cleanup_service.cancel_job(job_id) is True
        s = wait_for_done(cleanup_service.get_cleanup_progress, job_id)
        assert s["state"] == "cancelled", s
        remaining = list((teslacam / "RecentClips").glob("*.mp4"))
        assert len(remaining) > 0, "取消后应有文件残留"

    def test_boot_cleanup_disabled_by_default(self, state_dir, teslacam):
        assert cleanup_service.boot_cleanup_if_enabled() is None
