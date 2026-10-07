"""演示服务层：真实服务（web/services/，Workstream C）缺失或异常时的回退。

每个领域对象的方法签名严格遵循 docs/INTERFACES.md。
当 web/services/<domain>.py 存在且方法齐全时，_services.svc() 会优先使用真实实现。
"""
import os
import tempfile
import threading
import time

_CAMERAS = ["front", "left_repeater", "left_pillar", "right_pillar", "right_repeater", "back"]

_FOLDERS = ["RecentClips", "SavedClips", "SentryClips"]


def _demo_dir():
    d = os.path.join(tempfile.gettempdir(), "teslausb-cn-demo")
    os.makedirs(d, exist_ok=True)
    return d


def _dummy_mp4(name):
    """生成一个小的占位 mp4（仅用于演示 Range/206 逻辑，不可播放）。"""
    p = os.path.join(_demo_dir(), name)
    if not os.path.exists(p):
        with open(p, "wb") as f:
            f.write(b"\x00" * 65536)
    return p


class _Videos:
    _favorites = set()

    def list_videos(self, folder=None, favorite_only=False):
        items = []
        samples = [
            ("2026-10-07_09-15-32", "SentryClips", "2026-10-07 09:15", False),
            ("2026-10-07_08-02-11", "RecentClips", "2026-10-07 08:02", False),
            ("2026-10-06_18-44-05", "SentryClips", "2026-10-06 18:44", True),
            ("2026-10-06_14-32-11", "RecentClips", "2026-10-06 14:32", False),
            ("2026-10-05_21-10-48", "SavedClips", "2026-10-05 21:10", True),
            ("2026-10-05_07-55-20", "RecentClips", "2026-10-05 07:55", False),
        ]
        for sid, fld, t, fav in samples:
            if folder and fld != folder:
                continue
            is_fav = fav or sid in self._favorites
            if favorite_only and not is_fav:
                continue
            items.append({
                "name": sid, "folder": fld, "time": t, "duration_s": 60,
                "cameras": list(_CAMERAS), "size_mb": 42.5,
                "thumbnail_url": "", "favorite": is_fav,
            })
        return items

    def get_video_path(self, folder, name):
        return _dummy_mp4(f"{name}.mp4")

    def get_folder_stats(self):
        return {
            "RecentClips": {"count": 1248, "size_gb": 18.6},
            "SavedClips": {"count": 36, "size_gb": 2.1},
            "SentryClips": {"count": 112, "size_gb": 6.8},
        }

    def estimate_recording_time(self):
        return {"hours": 42, "method": "按已有视频平均码率估算", "confidence": "high"}

    def toggle_favorite(self, name, folder):
        if name in self._favorites:
            self._favorites.discard(name)
            return False
        self._favorites.add(name)
        return True

    def delete_videos(self, names, folder):
        return {"deleted": len(names)}


class _Chimes:
    def __init__(self):
        self._chimes = [
            {"name": "custom_chime_03.wav", "duration_s": 8, "is_active": True},
            {"name": "morning_bird.wav", "duration_s": 6, "is_active": False},
            {"name": "festival_drum.wav", "duration_s": 10, "is_active": False},
        ]
        self._schedules = [
            {"id": "s1", "name": "工作日早晨", "type": "weekly", "chime": "morning_bird.wav",
             "days": ["一", "二", "三", "四", "五"], "enabled": True},
            {"id": "s2", "name": "春节", "type": "holiday", "chime": "festival_drum.wav",
             "holiday": "春节", "enabled": True},
        ]
        self._groups = [
            {"id": "g1", "name": "日常随机池", "chimes": ["custom_chime_03.wav", "morning_bird.wav"]},
        ]
        self._random = {"enabled": True, "source_group_id": "g1"}
        self._seq = 100

    def list_chimes(self):
        return list(self._chimes)

    def set_active(self, name):
        for c in self._chimes:
            c["is_active"] = (c["name"] == name)
        return True

    def upload_chime(self, file, normalize_volume=True):
        name = getattr(file, "filename", "uploaded.wav") or "uploaded.wav"
        self._chimes.append({"name": name, "duration_s": 8, "is_active": False})
        return {"name": name}

    def delete_chime(self, name):
        self._chimes = [c for c in self._chimes if c["name"] != name]
        return True

    def list_schedules(self):
        return list(self._schedules)

    def create_schedule(self, data):
        self._seq += 1
        s = {"id": f"s{self._seq}", "enabled": True}
        s.update(data)
        self._schedules.append(s)
        return s

    def update_schedule(self, sid, data):
        for s in self._schedules:
            if s["id"] == sid:
                s.update(data)
                return s
        return None

    def delete_schedule(self, sid):
        self._schedules = [s for s in self._schedules if s["id"] != sid]
        return True

    def list_groups(self):
        return list(self._groups)

    def create_group(self, name):
        self._seq += 1
        g = {"id": f"g{self._seq}", "name": name, "chimes": []}
        self._groups.append(g)
        return g

    def add_to_group(self, group_id, chime):
        for g in self._groups:
            if g["id"] == group_id and chime not in g["chimes"]:
                g["chimes"].append(chime)
        return True

    def remove_from_group(self, group_id, chime):
        for g in self._groups:
            if g["id"] == group_id and chime in g["chimes"]:
                g["chimes"].remove(chime)
        return True

    def delete_group(self, gid):
        self._groups = [g for g in self._groups if g["id"] != gid]
        return True

    def set_random_source(self, group_id):
        self._random["source_group_id"] = group_id
        return True

    def get_random_mode(self):
        return dict(self._random)

    def set_random_mode(self, enabled):
        self._random["enabled"] = bool(enabled)
        return dict(self._random)


class _Lightshows:
    def __init__(self):
        self._shows = [
            {"name": "圣诞灯光秀", "has_fseq": True, "has_mp3": True},
            {"name": "新年灯光秀", "has_fseq": True, "has_mp3": True},
        ]

    def list_shows(self):
        return list(self._shows)

    def upload_show(self, fseq_file, mp3_file):
        name = (getattr(mp3_file, "filename", "") or "new_show.mp3").rsplit(".", 1)[0]
        self._shows.append({"name": name, "has_fseq": True, "has_mp3": True})
        return {"name": name}

    def delete_show(self, name):
        self._shows = [s for s in self._shows if s["name"] != name]
        return True


class _System:
    def get_status(self):
        return {"mode": "present", "temp_c": 48, "throttled": False,
                "wifi": {"ssid": "家里 WiFi", "signal": 82}, "version": "v1.0-cn"}

    def get_storage(self):
        return {"teslacam": {"total_gb": 400, "used_gb": 248},
                "lightshow": {"total_gb": 20, "used_gb": 3}}

    def switch_mode(self, target):
        return {"mode": target}

    def get_logs(self, level=None):
        logs = [
            {"time": "10-07 09:12:03", "level": "info", "msg": "归档完成 48 个文件"},
            {"time": "10-07 09:10:44", "level": "info", "msg": "WiFi 已连接（家里 WiFi）"},
            {"time": "10-07 08:58:21", "level": "warning", "msg": "CPU 温度 68°C，暂停后台任务"},
            {"time": "10-07 08:30:00", "level": "info", "msg": "开机自检完成"},
            {"time": "10-06 22:14:56", "level": "error", "msg": "NAS 连接超时，已重试"},
            {"time": "10-06 22:14:40", "level": "info", "msg": "开始备份到 NAS"},
        ]
        if level and level != "all":
            logs = [l for l in logs if l["level"] == level]
        return logs

    def download_logs(self):
        p = os.path.join(_demo_dir(), "teslausb-cn.log")
        with open(p, "w", encoding="utf-8") as f:
            for l in self.get_logs():
                f.write(f"{l['time']} [{l['level']}] {l['msg']}\n")
        return p

    def clear_logs(self):
        return True


class _Jobs:
    """演示用进度任务：每次轮询推进，模拟长耗时动作。"""

    def __init__(self):
        self._jobs = {}
        self._lock = threading.Lock()
        self._seq = 0

    def start(self, kind, total):
        with self._lock:
            self._seq += 1
            jid = f"job{self._seq}"
            self._jobs[jid] = {"state": "running", "percent": 0,
                               "current": 0, "total": total, "kind": kind}
            return jid

    def progress(self, jid):
        with self._lock:
            j = self._jobs.get(jid)
            if not j:
                return {"state": "failed", "percent": 0, "message": "任务不存在"}
            if j["state"] == "running":
                j["current"] = min(j["total"], j["current"] + max(1, j["total"] // 8))
                j["percent"] = int(j["current"] / j["total"] * 100)
                if j["current"] >= j["total"]:
                    j["state"] = "done"
                    j["percent"] = 100
            return {"state": j["state"], "percent": j["percent"],
                    "current": j["current"], "total": j["total"],
                    "message": ""}

    def cancel(self, jid):
        with self._lock:
            j = self._jobs.get(jid)
            if j and j["state"] == "running":
                j["state"] = "cancelled"
            return True


_jobs = _Jobs()


class _Backup:
    def __init__(self):
        self._history = [
            {"time": "2026-10-05 02:00", "files": 48, "size_gb": 2.1, "status": "成功"},
            {"time": "2026-10-04 02:00", "files": 52, "size_gb": 2.4, "status": "成功"},
        ]

    def configure_nas(self, cfg):
        self._last_cfg = dict(cfg)
        return True

    def start_backup(self):
        return _jobs.start("backup", 48)

    def get_backup_progress(self, job_id):
        return _jobs.progress(job_id)

    def get_job_progress(self, job_id):
        return _jobs.progress(job_id)

    def get_backup_history(self):
        return list(self._history)


class _Cleanup:
    def get_retention_config(self):
        return {"run_on_boot": True, "recentclips_retention_days": 7,
                "savedclips_retention_days": 0, "sentryclips_retention_days": 30,
                "protect_last_hour": True}

    def set_retention_config(self, cfg):
        return True

    def preview_cleanup(self):
        return {"files": 12, "size_gb": 3.2}

    def run_cleanup(self):
        return _jobs.start("cleanup", 12)

    def get_job_progress(self, job_id):
        return _jobs.progress(job_id)


class _Wifi:
    def get_wifi_status(self):
        return {"ssid": "家里 WiFi", "signal": 82, "mode": "client"}

    def scan_networks(self):
        return [
            {"ssid": "家里 WiFi", "signal": 82, "secured": True},
            {"ssid": "Neighbor-5G", "signal": 35, "secured": True},
            {"ssid": "Tesla-Guest", "signal": 61, "secured": False},
        ]

    def connect(self, ssid, password):
        return {"ok": True, "ssid": ssid}

    def forget(self, ssid):
        return True

    def saved_networks(self):
        return [{"ssid": "家里 WiFi"}, {"ssid": "公司 WiFi"}]

    def set_ap_mode(self, enabled):
        return {"mode": "ap" if enabled else "client"}

    def get_ap_config(self):
        return {"ssid": "TeslaUSB-CN", "password": "teslausb123"}

    def set_ap_config(self, cfg):
        return True


videos = _Videos()
chimes = _Chimes()
lightshows = _Lightshows()
system = _System()
backup = _Backup()
cleanup = _Cleanup()
wifi = _Wifi()
jobs = _jobs
