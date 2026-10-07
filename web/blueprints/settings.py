from flask import (Blueprint, flash, jsonify, redirect, render_template, request,
                   send_file, url_for)

from web import config as app_config
from web.blueprints._services import call, find
from web.i18n import _

bp = Blueprint("settings", __name__, url_prefix="/settings")

MIRRORS = ("tsinghua", "ustc", "aliyun")


@bp.route("/")
def index():
    wifi_status = call("wifi", "get_wifi_status")
    networks = call("wifi", "scan_networks")
    saved = call("wifi", "saved_networks")
    ap_config = call("wifi", "get_ap_config")
    history = call("backup", "get_backup_history")
    retention = call("cleanup", "get_retention_config")
    return render_template("settings.html", active_tab="settings",
                           wifi_status=wifi_status, networks=networks, saved=saved,
                           ap_config=ap_config, history=history, retention=retention,
                           mirror=app_config.get("mirror", "auto"),
                           mirrors=MIRRORS,
                           timezone=app_config.tesla_timezone(),
                           version=call("system", "get_status").get("version", ""))


# ---- WiFi ----
@bp.route("/api/wifi/connect", methods=["POST"])
def wifi_connect():
    ssid = request.form.get("ssid", "")
    password = request.form.get("password", "")
    if ssid:
        call("wifi", "connect", ssid, password)
        flash(_("wifi_connected_toast", ssid=ssid))
    return redirect(url_for("settings.index"))


@bp.route("/api/wifi/forget", methods=["POST"])
def wifi_forget():
    ssid = request.form.get("ssid", "")
    if ssid:
        call("wifi", "forget", ssid)
        flash(_("saved"))
    return redirect(url_for("settings.index"))


@bp.route("/api/wifi/ap", methods=["POST"])
def wifi_ap():
    enabled = request.form.get("enabled") == "on"
    call("wifi", "set_ap_mode", enabled)
    flash(_("saved"))
    return redirect(url_for("settings.index"))


@bp.route("/api/wifi/ap_config", methods=["POST"])
def wifi_ap_config():
    call("wifi", "set_ap_config", {
        "ssid": request.form.get("ssid", ""),
        "password": request.form.get("password", ""),
    })
    flash(_("saved"))
    return redirect(url_for("settings.index"))


# ---- 镜像源 ----
@bp.route("/api/mirror", methods=["POST"])
def set_mirror():
    # Phase 1：演示层确认；真实落盘由 Workstream A 的 setup/config 工具链完成
    flash(_("saved"))
    return redirect(url_for("settings.index"))


# ---- NAS 备份 ----
@bp.route("/api/nas", methods=["POST"])
def nas_save():
    call("backup", "configure_nas", {
        "protocol": request.form.get("protocol", "smb"),
        "host": request.form.get("host", ""),
        "port": int(request.form.get("port") or 445),
        "username": request.form.get("username", ""),
        "password": request.form.get("password", ""),
        "delete_after_backup": request.form.get("delete_after_backup") == "on",
    })
    flash(_("nas_saved"))
    return redirect(url_for("settings.index"))


@bp.route("/api/backup/start", methods=["POST"])
def backup_start():
    job_id = call("backup", "start_backup")
    return jsonify({"job_id": job_id})


# ---- 自动清理 ----
@bp.route("/api/cleanup", methods=["POST"])
def cleanup_save():
    call("cleanup", "set_retention_config", {
        "run_on_boot": request.form.get("run_on_boot") == "on",
        "recentclips_retention_days": int(request.form.get("recentclips_retention_days") or 0),
        "savedclips_retention_days": int(request.form.get("savedclips_retention_days") or 0),
        "sentryclips_retention_days": int(request.form.get("sentryclips_retention_days") or 0),
    })
    flash(_("saved"))
    return redirect(url_for("settings.index"))


@bp.route("/api/cleanup/preview")
def cleanup_preview():
    return jsonify(call("cleanup", "preview_cleanup"))


@bp.route("/api/cleanup/run", methods=["POST"])
def cleanup_run():
    job_id = call("cleanup", "run_cleanup")
    return jsonify({"job_id": job_id})


# ---- 进度任务通用（契约：get_job_progress(job_id)） ----
@bp.route("/api/jobs/<job_id>")
def job_progress(job_id):
    for domain in ("backup", "cleanup"):
        fn = find(domain, "get_job_progress")
        if fn is None:
            continue
        try:
            progress = fn(job_id)
        except Exception:
            continue
        if isinstance(progress, dict) and "state" in progress:
            return jsonify(progress)
    return jsonify({"state": "failed", "percent": 0, "message": ""})


@bp.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def job_cancel(job_id):
    from web.blueprints import _mock
    _mock.jobs.cancel(job_id)
    return jsonify({"ok": True})


# ---- 系统日志 ----
@bp.route("/logs")
def logs():
    level = request.args.get("level", "all")
    entries = call("system", "get_logs", level if level != "all" else None)
    return render_template("logs.html", active_tab="settings",
                           entries=entries, level=level)


@bp.route("/logs/download")
def logs_download():
    path = call("system", "download_logs")
    return send_file(path, as_attachment=True, download_name="teslausb-cn.log")


@bp.route("/logs/clear", methods=["POST"])
def logs_clear():
    call("system", "clear_logs")
    flash(_("log_cleared"))
    return redirect(url_for("settings.logs"))
