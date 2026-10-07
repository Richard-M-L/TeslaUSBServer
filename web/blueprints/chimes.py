from flask import Blueprint, flash, redirect, render_template, request, url_for

from web.blueprints._services import call
from web.i18n import _

bp = Blueprint("chimes", __name__, url_prefix="/chimes")

SCHEDULE_TYPES = ("weekly", "date", "holiday", "recurring")


@bp.route("/")
def index():
    chimes = call("chimes", "list_chimes")
    schedules = call("chimes", "list_schedules")
    groups = call("chimes", "list_groups")
    random_mode = call("chimes", "get_random_mode")
    active = next((c for c in chimes if c.get("is_active")), None)
    return render_template("chimes.html", active_tab="chimes", chimes=chimes,
                           active=active, schedules=schedules, groups=groups,
                           random_mode=random_mode, schedule_types=SCHEDULE_TYPES)


@bp.route("/api/active", methods=["POST"])
def set_active():
    name = request.form.get("name", "")
    if name:
        call("chimes", "set_active", name)
        flash(_("chime_set"))
    return redirect(url_for("chimes.index"))


@bp.route("/api/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    normalize = request.form.get("normalize") == "on"
    if not f or not f.filename:
        flash(_("invalid_file"))
    else:
        call("chimes", "upload_chime", f, normalize_volume=normalize)
        flash(_("chime_uploaded"))
    return redirect(url_for("chimes.index"))


@bp.route("/api/delete", methods=["POST"])
def delete():
    name = request.form.get("name", "")
    if name:
        call("chimes", "delete_chime", name)
        flash(_("chime_deleted"))
    return redirect(url_for("chimes.index"))


@bp.route("/api/schedules", methods=["POST"])
def create_schedule():
    stype = request.form.get("type", "weekly")
    if stype not in SCHEDULE_TYPES:
        stype = "weekly"
    data = {
        "name": request.form.get("name", ""),
        "type": stype,
        "chime": request.form.get("chime", ""),
        "enabled": request.form.get("enabled") == "on",
        "days": request.form.getlist("days"),
        "date": request.form.get("date", ""),
        "holiday": request.form.get("holiday", ""),
    }
    call("chimes", "create_schedule", data)
    flash(_("schedule_created"))
    return redirect(url_for("chimes.index"))


@bp.route("/api/schedules/<sid>", methods=["POST"])
def update_schedule(sid):
    stype = request.form.get("type", "weekly")
    if stype not in SCHEDULE_TYPES:
        stype = "weekly"
    data = {
        "name": request.form.get("name", ""),
        "type": stype,
        "chime": request.form.get("chime", ""),
        "enabled": request.form.get("enabled") == "on",
        "days": request.form.getlist("days"),
        "date": request.form.get("date", ""),
        "holiday": request.form.get("holiday", ""),
    }
    call("chimes", "update_schedule", sid, data)
    flash(_("schedule_updated"))
    return redirect(url_for("chimes.index"))


@bp.route("/api/schedules/<sid>/toggle", methods=["POST"])
def toggle_schedule(sid):
    schedules = call("chimes", "list_schedules")
    s = next((x for x in schedules if x["id"] == sid), None)
    if s:
        call("chimes", "update_schedule", sid, {"enabled": not s.get("enabled")})
        flash(_("saved"))
    return redirect(url_for("chimes.index"))


@bp.route("/api/schedules/<sid>/delete", methods=["POST"])
def delete_schedule(sid):
    call("chimes", "delete_schedule", sid)
    flash(_("schedule_deleted"))
    return redirect(url_for("chimes.index"))


@bp.route("/api/groups", methods=["POST"])
def create_group():
    name = request.form.get("name", "")
    if name:
        call("chimes", "create_group", name)
        flash(_("group_created"))
    return redirect(url_for("chimes.index"))


@bp.route("/api/groups/<gid>/add", methods=["POST"])
def add_to_group(gid):
    chime = request.form.get("chime", "")
    if chime:
        call("chimes", "add_to_group", gid, chime)
        flash(_("saved"))
    return redirect(url_for("chimes.index"))


@bp.route("/api/groups/<gid>/remove", methods=["POST"])
def remove_from_group(gid):
    chime = request.form.get("chime", "")
    if chime:
        call("chimes", "remove_from_group", gid, chime)
        flash(_("saved"))
    return redirect(url_for("chimes.index"))


@bp.route("/api/groups/<gid>/delete", methods=["POST"])
def delete_group(gid):
    call("chimes", "delete_group", gid)
    flash(_("saved"))
    return redirect(url_for("chimes.index"))


@bp.route("/api/groups/<gid>/source", methods=["POST"])
def set_random_source(gid):
    call("chimes", "set_random_source", gid)
    flash(_("saved"))
    return redirect(url_for("chimes.index"))


@bp.route("/api/random_mode", methods=["POST"])
def set_random_mode():
    enabled = request.form.get("enabled") == "on"
    call("chimes", "set_random_mode", enabled)
    flash(_("random_mode_on") if enabled else _("random_mode_off"))
    return redirect(url_for("chimes.index"))
