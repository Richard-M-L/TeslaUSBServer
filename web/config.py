"""读取仓库根目录 config.yaml。

优先用 PyYAML；未安装时用内置的最小解析器（支持本配置的子集：
# 注释、key: value、两空格缩进的一级嵌套、引号/布尔/数字字面量）。
"""
import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_PATH = os.path.join(_REPO_ROOT, "config.yaml")
_cache = None


def _simple_parse(text):
    """最小 YAML 子集解析，仅用于本项目 config.yaml 的结构。"""
    root = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, _, val = line.strip().partition(":")
        key, val = key.strip(), val.strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if val == "":
            node = {}
            parent[key] = node
            stack.append((indent, node))
        else:
            parent[key] = _scalar(val)
    return root


def _scalar(val):
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        return val[1:-1]
    low = val.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def load_config():
    global _cache
    if _cache is None:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            text = f.read()
        try:
            import yaml
            _cache = yaml.safe_load(text) or {}
        except ImportError:
            _cache = _simple_parse(text)
    return _cache


def get(path, default=None):
    cur = load_config()
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def tesla_timezone():
    """车机时区：所有时间显示一律用它显式转换，禁止依赖系统时区。"""
    return get("tesla_timezone", "Asia/Shanghai")
