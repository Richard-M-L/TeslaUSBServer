from flask import Blueprint, flash, redirect, render_template, request, url_for

from web.blueprints._services import call
from web.i18n import _

bp = Blueprint("lightshows", __name__, url_prefix="/lightshows")


@bp.route("/")
def index():
    shows = call("lightshows", "list_shows")
    return render_template("lightshows.html", active_tab="lightshows", shows=shows)


@bp.route("/api/upload", methods=["POST"])
def upload():
    fseq = request.files.get("fseq")
    mp3 = request.files.get("mp3")
    if not fseq or not mp3 or not fseq.filename or not mp3.filename:
        flash(_("invalid_file"))
    else:
        call("lightshows", "upload_show", fseq, mp3)
        flash(_("show_uploaded"))
    return redirect(url_for("lightshows.index"))


@bp.route("/api/delete", methods=["POST"])
def delete():
    name = request.form.get("name", "")
    if name:
        call("lightshows", "delete_show", name)
        flash(_("show_deleted"))
    return redirect(url_for("lightshows.index"))
