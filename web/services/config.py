"""集中配置读取：仓库根 ``config.yaml`` + 环境变量覆盖。

来源：TeslaUSB-CN Phase 1 规划——安装脚本与 Web 应用共享同一份
``config.yaml``（仓库根，即本文件上两级目录）。本模块只读配置、
不写配置；纯 Python，不依赖 Flask。

环境变量（用于测试与特殊部署覆盖）：
- ``TESLAUSB_CONFIG``：config.yaml 路径覆盖（默认仓库根 config.yaml）
- ``TESLAUSB_HOME``：TeslaUSB 工作目录（默认 ``/home/pi/TeslaUSB``）
- ``TESLAUSB_MNT_DIR``：USB 挂载目录（默认 ``/mnt/teslausb``）
"""

import logging
import os

import yaml

logger = logging.getLogger(__name__)

# 本文件位于 <repo>/web/services/config.py，上三级即仓库根
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_CONFIG_PATH = os.path.join(_REPO_ROOT, "config.yaml")

# 按路径缓存解析结果（config.yaml 很小；缓存键为路径，环境变量覆盖路径时自动隔离）
_CONFIG_CACHE = {}


def _config_path() -> str:
    return os.environ.get("TESLAUSB_CONFIG", _DEFAULT_CONFIG_PATH)


def reload_config() -> None:
    """清空配置缓存，下次 ``load_config()`` 重新从磁盘读取。"""
    _CONFIG_CACHE.clear()


def load_config() -> dict:
    """读取并解析 config.yaml，返回 dict。

    文件缺失或解析失败时记中文 warning 并返回空 dict（调用方走默认值），
    不抛异常——Pi 首次安装前 Web 可能先于配置生成而启动。
    """
    path = _config_path()
    if path in _CONFIG_CACHE:
        return _CONFIG_CACHE[path]

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning("配置文件不存在：%s，将使用内置默认值", path)
        data = {}
    except yaml.YAMLError as e:
        logger.warning("配置文件解析失败：%s（%s），将使用内置默认值", path, e)
        data = {}
    except OSError as e:
        logger.warning("配置文件读取失败：%s（%s），将使用内置默认值", path, e)
        data = {}

    if not isinstance(data, dict):
        logger.warning("配置文件顶层不是 mapping：%s，将使用内置默认值", path)
        data = {}

    _CONFIG_CACHE[path] = data
    return data


def get_tesla_timezone_name() -> str:
    """车机时区名（config.yaml ``tesla_timezone``，默认 ``Asia/Shanghai``）。"""
    name = load_config().get("tesla_timezone") or "Asia/Shanghai"
    return str(name)


def get_tesla_timezone():
    """车机时区 ``ZoneInfo``（时区名非法时回退 Asia/Shanghai 并记 warning）。"""
    from zoneinfo import ZoneInfo

    name = get_tesla_timezone_name()
    try:
        return ZoneInfo(name)
    except Exception:
        logger.warning("时区配置非法：%s，回退为 Asia/Shanghai", name)
        return ZoneInfo("Asia/Shanghai")


def get_home_dir() -> str:
    """TeslaUSB 工作目录：``$TESLAUSB_HOME`` 或 ``/home/pi/TeslaUSB``。"""
    return os.environ.get("TESLAUSB_HOME", "/home/pi/TeslaUSB")


def get_mount_dir() -> str:
    """USB 分区挂载根目录：``$TESLAUSB_MNT_DIR`` 或 ``/mnt/teslausb-cn``。

    默认值与 setup.sh 的 MNT_DIR 保持一致（安装脚本决定真实挂载点）。
    """
    return os.environ.get("TESLAUSB_MNT_DIR", "/mnt/teslausb-cn")


def get_teslacam_root() -> str:
    """TeslaCam 分区根目录：``<mount>/part1/TeslaCam``。"""
    return os.path.join(get_mount_dir(), "part1", "TeslaCam")


def get_lightshow_root() -> str:
    """LightShow 分区根目录：``<mount>/part2``。"""
    return os.path.join(get_mount_dir(), "part2")


def get_state_dir() -> str:
    """状态目录：``<home>/state``，不存在则创建。"""
    state_dir = os.path.join(get_home_dir(), "state")
    os.makedirs(state_dir, exist_ok=True)
    return state_dir


def get_config_value(*keys, default=None):
    """按嵌套 key 取配置值，如 ``get_config_value("cleanup", "run_on_boot", default=False)``。

    任一 key 缺失或中间层不是 dict 时返回 ``default``。
    """
    node = load_config()
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node
