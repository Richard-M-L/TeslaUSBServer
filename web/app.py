"""TeslaUSB-CN Web 应用工厂。"""
import os
import secrets

from flask import Flask, render_template

from web import config as app_config
from web.i18n import _


def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = os.environ.get("TESLAUSB_CN_SECRET") or secrets.token_hex(32)

    # Jinja 全局 _()：模板一律经它取中文文案
    app.jinja_env.globals["_"] = _
    app.jinja_env.globals["tesla_timezone"] = app_config.tesla_timezone()

    from web.blueprints import chimes, home, lightshows, settings, videos
    app.register_blueprint(home.bp)
    app.register_blueprint(videos.bp)
    app.register_blueprint(chimes.bp)
    app.register_blueprint(lightshows.bp)
    app.register_blueprint(settings.bp)

    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", active_tab="",
                               code=404, message=_("error_404")), 404

    @app.errorhandler(500)
    def internal_error(e):
        return render_template("error.html", active_tab="",
                               code=500, message=_("error_500")), 500

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=5000)
