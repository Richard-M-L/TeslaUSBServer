from flask import Blueprint, flash, redirect, render_template, request, url_for

from web.blueprints._services import call
from web.i18n import _

bp = Blueprint("home", __name__)


@bp.route("/")
def index():
    status = call("system", "get_status")
    storage = call("system", "get_storage")
    folder_stats = call("videos", "get_folder_stats")
    estimate = call("videos", "estimate_recording_time")
    return render_template("home.html", active_tab="home", status=status,
                           storage=storage, folder_stats=folder_stats,
                           estimate=estimate)


@bp.route("/api/mode", methods=["POST"])
def switch_mode():
    target = request.form.get("target", "present")
    if target not in ("present", "edit"):
        target = "present"
    call("system", "switch_mode", target)
    flash(_("saved"))
    return redirect(url_for("home.index"))
