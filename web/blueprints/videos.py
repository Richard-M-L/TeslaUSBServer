import os
import re

from flask import (Blueprint, Response, abort, flash, redirect, render_template,
                   request, url_for)

from web.blueprints._services import call, find
from web.i18n import _

bp = Blueprint("videos", __name__, url_prefix="/videos")

FOLDERS = ("RecentClips", "SavedClips", "SentryClips")
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-.]+$")


@bp.route("/")
def index():
    folder = request.args.get("folder") or None
    if folder not in FOLDERS:
        folder = None
    favorite_only = request.args.get("fav") == "1"
    videos = call("videos", "list_videos", folder=folder, favorite_only=favorite_only)
    groups = {}
    for v in videos:
        day = v["time"].split(" ")[0]
        groups.setdefault(day, []).append(v)
    return render_template("videos.html", active_tab="videos", videos=videos,
                           groups=groups, folder=folder, favorite_only=favorite_only,
                           folders=FOLDERS)


@bp.route("/player")
def player():
    name = request.args.get("name", "")
    folder = request.args.get("folder", "")
    if folder not in FOLDERS or not _NAME_RE.match(name):
        abort(404)
    videos = call("videos", "list_videos", folder=folder)
    video = next((v for v in videos if v["name"] == name), None)
    if not video:
        video = {"name": name, "folder": folder, "time": name.replace("_", " ")[:16],
                 "duration_s": 60, "cameras": ["front", "left_repeater", "left_pillar",
                 "right_pillar", "right_repeater", "back"],
                 "size_mb": 42.5, "favorite": False}
    cameras = video.get("cameras") or ["front", "left_repeater", "left_pillar",
                                       "right_pillar", "right_repeater", "back"]
    return render_template("player.html", active_tab="videos", video=video,
                           cameras=cameras)


@bp.route("/api/favorite", methods=["POST"])
def toggle_favorite():
    pairs = [p for p in request.form.get("names", "").split(",") if p]
    single = request.form.get("name", "")
    if single:
        pairs.append(request.form.get("folder", "") + ":" + single)
    results = []
    for p in pairs:
        if ":" not in p:
            continue
        folder, name = p.split(":", 1)
        if folder not in FOLDERS or not _NAME_RE.match(name):
            continue
        results.append(call("videos", "toggle_favorite", name, folder))
    if results:
        flash(_("favorited") if any(results) else _("unfavorited"))
    return redirect(request.form.get("next") or url_for("videos.index"))


@bp.route("/api/delete", methods=["POST"])
def delete_videos():
    pairs = [p for p in request.form.get("names", "").split(",") if p]
    by_folder = {}
    for p in pairs:
        if ":" not in p:
            continue
        folder, name = p.split(":", 1)
        if folder in FOLDERS and _NAME_RE.match(name):
            by_folder.setdefault(folder, []).append(name)
    total = 0
    for folder, names in by_folder.items():
        result = call("videos", "delete_videos", names, folder) or {}
        total += result.get("deleted", len(names))
    if total:
        flash(_("deleted", n=total))
    return redirect(url_for("videos.index"))


def _resolve_path(folder, name):
    """解析视频文件路径并做路径穿越校验。"""
    if folder not in FOLDERS or not _NAME_RE.match(name) or ".." in name:
        return None
    getter = find("videos", "get_video_path")
    if getter is None:
        return None
    try:
        path = getter(folder, name)
    except Exception:
        return None
    if not path or not os.path.isfile(path):
        return None
    return path


@bp.route("/stream/<folder>/<name>")
def stream(folder, name):
    """视频流：手工实现 HTTP Range/206，支持浏览器 seek。"""
    path = _resolve_path(folder, name)
    if not path:
        abort(404)
    size = os.path.getsize(path)
    start, end, status = 0, size - 1, 200
    range_h = request.headers.get("Range")
    if range_h:
        m = re.match(r"bytes=(\d*)-(\d*)", range_h.strip())
        if m:
            s, e = m.groups()
            if s == "" and e != "":
                start = max(0, size - int(e))
            elif s != "":
                start = int(s)
                end = int(e) if e != "" else size - 1
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
            status = 206
    length = end - start + 1

    def gen():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    resp = Response(gen(), status=status, mimetype="video/mp4",
                    direct_passthrough=True)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(length)
    if status == 206:
        resp.headers["Content-Range"] = "bytes {0}-{1}/{2}".format(start, end, size)
    return resp
