"""中文文案 i18n：自研 JSON 字典 + 全局 _()（审计 §3.3 方案）。

用法：
    from web.i18n import _
    _('video_overview')            # -> "视频总览"
    _('selected_n', n=3)           # -> "已选 3 个"（支持 str.format 占位）
"""
import json
import os

_LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")
_cache = {}


def _load():
    if "zh_CN" not in _cache:
        path = os.path.join(_LOCALES_DIR, "zh_CN.json")
        with open(path, encoding="utf-8") as f:
            _cache["zh_CN"] = json.load(f)
    return _cache["zh_CN"]


def _(key, **kwargs):
    s = _load().get(key, key)
    if kwargs:
        try:
            s = s.format(**kwargs)
        except Exception:
            pass
    return s
