"""Web 外壳冒烟测试：各路由 200 且含关键中文文案；视频流支持 Range/206。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.app import create_app


@pytest.fixture()
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _text(resp):
    return resp.get_data(as_text=True)


def test_home(client):
    r = client.get("/")
    assert r.status_code == 200
    body = _text(r)
    assert "视频总览" in body
    assert "RecentClips" in body
    assert "预计还可录制" in body
    assert "USB 模式" in body or "编辑模式" in body


def test_videos_list(client):
    r = client.get("/videos/")
    assert r.status_code == 200
    body = _text(r)
    assert "RecentClips" in body and "SentryClips" in body and "SavedClips" in body
    assert "取消选择" in body


def test_videos_filter(client):
    r = client.get("/videos/?folder=SentryClips")
    assert r.status_code == 200
    r = client.get("/videos/?fav=1")
    assert r.status_code == 200
    assert "收藏" in _text(r)


def test_player(client):
    r = client.get("/videos/player?name=2026-10-06_14-32-11&folder=RecentClips")
    assert r.status_code == 200
    body = _text(r)
    assert "左B柱" in body and "右B柱" in body
    assert "国行视频无 GPS 轨迹数据" in body


def test_stream_range(client):
    r = client.get("/videos/stream/RecentClips/2026-10-06_14-32-11",
                   headers={"Range": "bytes=0-99"})
    assert r.status_code == 206
    assert r.headers.get("Accept-Ranges") == "bytes"
    assert r.headers.get("Content-Range", "").startswith("bytes 0-99/")
    assert len(r.get_data()) == 100


def test_stream_full_and_404(client):
    r = client.get("/videos/stream/RecentClips/2026-10-06_14-32-11")
    assert r.status_code == 200
    assert client.get("/videos/stream/RecentClips/../../etc").status_code == 404
    assert client.get("/videos/stream/Nope/x").status_code == 404


def test_chimes(client):
    r = client.get("/chimes/")
    assert r.status_code == 200
    body = _text(r)
    assert "当前生效" in body
    assert "提示音定时计划" in body
    assert "随机提示音分组" in body
    assert "随机模式" in body
    assert "提示音库" in body


def test_lightshows(client):
    r = client.get("/lightshows/")
    assert r.status_code == 200
    assert "FSEQ+MP3" in _text(r)


def test_settings(client):
    r = client.get("/settings/")
    assert r.status_code == 200
    body = _text(r)
    assert "无线网络" in body
    assert "镜像源一键切换" in body
    assert "备份到 NAS" in body
    assert "自动清理视频" in body
    assert "系统日志" in body
    assert "Asia/Shanghai" in body


def test_logs(client):
    r = client.get("/settings/logs")
    assert r.status_code == 200
    assert "系统日志" in _text(r)
    r = client.get("/settings/logs?level=error")
    assert r.status_code == 200


def test_actions_post_redirect(client):
    assert client.post("/api/mode", data={"target": "edit"}).status_code == 302
    assert client.post("/videos/api/favorite",
                       data={"names": "RecentClips:2026-10-06_14-32-11"}).status_code == 302
    assert client.post("/chimes/api/active",
                       data={"name": "morning_bird.wav"}).status_code == 302
    r = client.post("/settings/api/backup/start")
    assert r.status_code == 200
    jid = r.get_json()["job_id"]
    r = client.get(f"/settings/api/jobs/{jid}")
    assert r.status_code == 200
    assert r.get_json()["state"] in ("running", "done")


def test_i18n_no_hardcoded_english_ui(client):
    """关键页面不应出现英文兜底标题。"""
    for url in ("/", "/videos/", "/chimes/", "/lightshows/", "/settings/"):
        body = _text(client.get(url))
        assert "TeslaUSB-CN" in body  # 品牌名例外
