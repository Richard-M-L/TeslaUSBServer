# 服务接口契约（Workstream B/C 共同遵守）

Web 蓝图（B）只通过以下接口调用服务层（C 实现）。所有时间一律使用 `config.yaml` 的
`tesla_timezone`（ZoneInfo）显式转换，禁止依赖系统时区；所有用户可见字符串走
`web/locales/zh_CN.json` 的 `_()`，禁止硬编码中文之外的文案。

## videos（视频）

- `list_videos(folder=None, favorite_only=False)` → list[{name, folder, time(str, 车机本地时间), duration_s, cameras[], size_mb, thumbnail_url, favorite(bool)}]
  - folder ∈ {RecentClips, SavedClips, SentryClips}
  - cameras ∈ {front, left_repeater, right_repeater, left_pillar, right_pillar, back}（按实际存在的文件返回）
- `get_folder_stats()` → {RecentClips: {count, size_gb}, SavedClips: {...}, SentryClips: {...}}
- `estimate_recording_time()` → {hours, method, confidence}（参考 fork 的 estimate_recording_time）
- `toggle_favorite(name, folder)` / `delete_videos(names, folder)`（删除需调用方二次确认）
- 视频流走 HTTP Range/206（B 直接实现，不经过服务层）

## chimes（锁车提示音）

- `list_chimes()` → [{name, duration_s, is_active}]
- `set_active(name)` / `upload_chime(file, normalize_volume=True)` / `delete_chime(name)`
- `list_schedules()` / `create_schedule({name, type, chime, days/date/holiday, enabled})` / `update_schedule(id, ...)` / `delete_schedule(id)`
  - type ∈ {weekly, date, holiday, recurring}
- `list_groups()` / `create_group(name)` / `add_to_group(group_id, chime)` / `remove_from_group(...)` / `delete_group(id)` / `set_random_source(group_id)`
- `get_random_mode()` / `set_random_mode(enabled)` —— 开启后每次 Pi 启动时从随机源分组随机设为当前

## lightshows（灯光秀）

- `list_shows()` → [{name, has_fseq, has_mp3}]
- `upload_show(fseq_file, mp3_file)` / `delete_show(name)`

## system（系统）

- `get_status()` → {mode: present/edit, temp_c, throttled: bool, wifi: {ssid, signal}, version}
- `get_storage()` → {teslacam: {total_gb, used_gb}, lightshow: {...}}
- `switch_mode(target)` → 需调用方二次确认
- `get_logs(level=None)` / `download_logs()` / `clear_logs()`

## backup（备份）

- `configure_nas({protocol, host, port, username, password})`（密码加密存储）
- `start_backup()` → job_id；`get_backup_progress(job_id)` → {percent, current, total}
- `get_backup_history()` → [{time, files, size_gb, status}]
- `delete_after_backup` 开关由 config.yaml 控制，服务层执行时二次校验

## cleanup（清理）

- `get_retention_config()` / `set_retention_config({...})`
- `preview_cleanup()` → {files, size_gb} / `run_cleanup()` → job_id（进度同 backup）

## wifi（无线网络）

- `get_wifi_status()` → {ssid, signal, mode: client/ap}
- `scan_networks()` → [{ssid, signal, secured}]
- `connect(ssid, password)` / `forget(ssid)` / `saved_networks()`
- `set_ap_mode(enabled)` / `get_ap_config()` / `set_ap_config({ssid, password})`

## 进度任务通用约定

长耗时任务（备份/清理）返回 job_id，`get_job_progress(job_id)` →
{state: running/done/failed/cancelled, percent, message}，支持 `cancel_job(job_id)`。
