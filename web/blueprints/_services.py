"""服务层切换点（Workstream B/C 交界）。

调用按领域路由：默认使用 web/blueprints/_mock.py 的演示实现；
设置环境变量 TESLAUSB_CN_USE_REAL_SERVICES=1 后，按 CANDIDATES 顺序
优先尝试 Workstream C 的真实服务模块（多模块领域按方法逐个解析），
任一环节缺失或抛异常时降级到下一个候选，最终回退 mock，
保证页面始终可渲染。降级时记 warning 日志，便于联调时发现。
"""
import importlib
import logging
import os

log = logging.getLogger(__name__)

# 真实服务需显式启用：TESLAUSB_CN_USE_REAL_SERVICES=1。
# 默认走演示实现，保证 Phase 1 联调完成前页面渲染稳定、测试可重复。
_USE_REAL = os.environ.get("TESLAUSB_CN_USE_REAL_SERVICES") == "1"

# 领域 -> 候选模块名（按优先级；mock 永远在最后）
CANDIDATES = {
    "videos": ["videos", "video_service"],
    "chimes": ["chimes", "lock_chime_service", "chime_scheduler"],
    "lightshows": ["lightshows", "lightshow_service"],
    "system": ["system", "system_service"],
    "backup": ["backup", "backup_service", "rclone_service"],
    "cleanup": ["cleanup", "cleanup_service"],
    "wifi": ["wifi", "wifi_service"],
}

_mod_cache = {}


def _import(name):
    if name not in _mod_cache:
        try:
            _mod_cache[name] = importlib.import_module("web.services." + name)
        except Exception:
            _mod_cache[name] = None
    return _mod_cache[name]


def _candidates(domain):
    """该领域的候选模块对象列表（mock 永远最后）。

    默认只用 mock；设置 TESLAUSB_CN_USE_REAL_SERVICES=1 后，
    按 CANDIDATES 顺序优先尝试真实服务模块。
    """
    from web.blueprints import _mock
    mods = []
    if _USE_REAL:
        for name in CANDIDATES.get(domain, [domain]):
            m = _import(name)
            if m is not None:
                mods.append(m)
    mods.append(getattr(_mock, domain))
    return mods


def find(domain, func):
    """找第一个提供 func 的候选实现（不调用）。"""
    for mod in _candidates(domain):
        fn = getattr(mod, func, None)
        if fn is not None:
            return fn
    return None


def call(domain, func, *args, **kwargs):
    """按候选顺序调用；失败则降级到下一个，最终用 mock。"""
    from web.blueprints import _mock
    mock_mod = getattr(_mock, domain)
    last_err = None
    for mod in _candidates(domain):
        fn = getattr(mod, func, None)
        if fn is None:
            continue
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - 降级是设计行为
            last_err = e
            if mod is not mock_mod:
                log.warning("服务 %s.%s 降级: %s", domain, func, e)
            continue
    if last_err is not None:
        raise last_err
    raise AttributeError("no implementation for %s.%s" % (domain, func))


def using_mock(domain):
    """是否没有任何真实候选模块（全走演示实现）。"""
    return all(m is None for m in
               (_import(n) for n in CANDIDATES.get(domain, [domain])))
