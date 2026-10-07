"""NAS 备份连接服务（rclone 通用 remote 精简版）。

移植取舍（来源：fork ``scripts/web/services/cloud_rclone_service.py``，1148 行）：
- 只保留中国家庭 NAS 常见协议：``smb / nfs / sftp / webdav``；
  砍掉 S3 / B2 / Azure / Swift / FTP 等云厂商与小众后端。
- 砍掉 OAuth token 流程（onedrive / google-drive / dropbox）、token 刷新捕获、
  云盘容量查询、远程建文件夹、单事件归档等云厂商专属逻辑。
- 保留并收紧：rclone.conf 注入防护（``_reject_control_chars``）、密码经
  ``rclone obscure`` 处理后再写入临时配置文件、临时配置写临时目录且用后即删。
- 凭据加密存储改为本机随机密钥 + Fernet；cryptography 缺失时回退为
  base64 混淆并记中文警告——两种路径都不落明文。fork 用的是硬件绑定密钥，
  CN 版为简化部署改用 ``state/`` 下的随机密钥文件。
- 同步命令固定追加 ``--partial --inplace``（断点续传），大文件经 WiFi
  中断后可续传，这是 fork 归档 worker 没有但 NAS 备份必需的。

用户可见的日志与报错一律中文。
"""

import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile

from web.services.config import get_state_dir

logger = logging.getLogger(__name__)

# rclone remote 名（临时配置文件中的 section 名）
REMOTE_NAME = "teslausb-nas"

# 仅保留的四种 NAS 协议（砍掉云厂商后端）
SUPPORTED_PROTOCOLS = ("smb", "nfs", "sftp", "webdav")

# 各协议默认端口（表单未填 port 时使用）
DEFAULT_PORTS = {"smb": 445, "nfs": 2049, "sftp": 22, "webdav": 80}

# 需要经 `rclone obscure` 处理的密码字段
_OBSCURE_KEYS = ("pass",)

# 不能出现在 rclone 配置字段中的字符：换行/回车/NUL 会注入额外的
# 配置行（例如 smb 下覆盖 endpoint 重定向上传，或 sftp 下经 ssh
# 指令执行命令）。直接移植 fork 的防护。
_FORBIDDEN_FIELD_CHARS = ("\n", "\r", "\x00")

# 凭据文件名（state/ 下）
_CREDS_FILE = "nas_creds.enc"
_KEY_FILE = ".nas_key"


def _reject_control_chars(label, value):
    """字段含换行/回车/NUL 时抛 ValueError（防 rclone.conf 注入）。"""
    for ch in _FORBIDDEN_FIELD_CHARS:
        if ch in value:
            raise ValueError(
                "%s 含有非法控制字符（0x%02x），已拦截。" % (label, ord(ch))
            )


def _state_path(name):
    d = get_state_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


# ---------------------------------------------------------------------------
# 凭据加密（Fernet；cryptography 缺失时回退 base64 + 中文警告，绝不明文）
# ---------------------------------------------------------------------------

def _load_or_create_key():
    """读取或生成本机随机密钥（state/.nas_key，0600）。"""
    path = _state_path(_KEY_FILE)
    if os.path.isfile(path):
        with open(path, "rb") as f:
            return f.read().strip()
    # Fernet 需要 32 字节 urlsafe-base64 编码的密钥
    raw = base64.urlsafe_b64encode(os.urandom(32))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)
    return raw


class _FernetCipher:
    """cryptography 可用时的加密器。"""

    def __init__(self):
        from cryptography.fernet import Fernet
        self._fernet = Fernet(_load_or_create_key())

    def encrypt(self, data: bytes) -> bytes:
        return self._fernet.encrypt(data)

    def decrypt(self, data: bytes) -> bytes:
        return self._fernet.decrypt(data)


class _ObfuscatedCipher:
    """cryptography 缺失时的回退：base64 混淆（非加密）+ 中文警告。

    只是让密码不在磁盘上以明文出现，安全性低于 Fernet；安装
    python3-cryptography 后会自动升级为真正的加密。
    """

    _warned = False

    def __init__(self):
        if not _ObfuscatedCipher._warned:
            _ObfuscatedCipher._warned = True
            logger.warning(
                "未安装 cryptography，NAS 密码仅做 base64 混淆存储"
                "（非加密）。建议安装 python3-cryptography 以启用 Fernet 加密。"
            )

    def encrypt(self, data: bytes) -> bytes:
        return b"obf1:" + base64.b64encode(data)

    def decrypt(self, data: bytes) -> bytes:
        if not data.startswith(b"obf1:"):
            raise ValueError("凭据文件格式无法识别")
        return base64.b64decode(data[len(b"obf1:"):])


def _get_cipher():
    try:
        return _FernetCipher()
    except ImportError:
        return _ObfuscatedCipher()


def _creds_path():
    return _state_path(_CREDS_FILE)


def _persist_creds(creds):
    """加密并原子写入凭据文件。"""
    blob = _get_cipher().encrypt(json.dumps(creds).encode("utf-8"))
    path = _creds_path()
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    logger.info("NAS 凭据已加密保存（%s）。", creds.get("type", "?"))


def _load_creds():
    """解密读取凭据；失败返回空 dict。"""
    path = _creds_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "rb") as f:
            blob = f.read()
        creds = json.loads(_get_cipher().decrypt(blob).decode("utf-8"))
        return creds if isinstance(creds, dict) else {}
    except Exception as e:
        logger.error("读取 NAS 凭据失败：%s", e)
        return {}


# ---------------------------------------------------------------------------
# 配置（表单：protocol / host / port / username / password）
# ---------------------------------------------------------------------------

def _build_creds(protocol, host, port, username, password):
    """由表单字段拼出 rclone 后端凭据 dict（密码保持明文，加密存盘；
    写临时 conf 时才经 rclone obscure 处理）。"""
    for label, value in (("地址", host), ("用户名", username),
                         ("密码", password), ("协议", protocol)):
        _reject_control_chars(label, value)
    port = int(port)
    if not (1 <= port <= 65535):
        raise ValueError("端口号必须在 1-65535 之间。")

    if protocol == "smb":
        return {"type": "smb", "host": host, "port": str(port),
                "user": username, "pass": password}
    if protocol == "nfs":
        return {"type": "nfs", "host": host, "port": str(port),
                "user": username}
    if protocol == "sftp":
        return {"type": "sftp", "host": host, "port": str(port),
                "user": username, "pass": password}
    # webdav 用 url 而非 host/port
    url = host if "://" in host else "http://%s:%d/" % (host, port)
    return {"type": "webdav", "url": url, "user": username,
            "pass": password}


def configure_nas(cfg):
    """保存 NAS 连接配置并加密存储密码。

    Args:
        cfg: {protocol, host, port, username, password}
    Returns:
        True
    Raises:
        ValueError: 参数非法（中文信息）。
    """
    cfg = dict(cfg or {})
    protocol = str(cfg.get("protocol", "")).strip().lower()
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ValueError(
            "不支持的协议 %r，仅支持：%s。"
            % (cfg.get("protocol"), "、".join(SUPPORTED_PROTOCOLS))
        )
    host = str(cfg.get("host", "")).strip()
    if not host:
        raise ValueError("请填写 NAS 地址。")
    port = cfg.get("port") or DEFAULT_PORTS[protocol]
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("端口号必须是数字。")
    username = str(cfg.get("username", "")).strip()
    password = str(cfg.get("password", ""))

    creds = _build_creds(protocol, host, port, username, password)
    creds["_obscure_keys"] = ",".join(_OBSCURE_KEYS)
    creds["_source"] = "form"
    _persist_creds(creds)
    logger.info("NAS 配置已保存：%s://%s:%s，用户 %s。",
                protocol, host, port, username or "（匿名）")
    return True


def get_nas_config():
    """返回 NAS 配置（不含密码明文，供表单回显）。"""
    creds = _load_creds()
    if not creds:
        return {"protocol": "smb", "host": "", "port": 445,
                "username": "", "has_password": False, "configured": False}
    protocol = creds.get("type", "smb")
    host = creds.get("host", "")
    port = int(creds.get("port") or DEFAULT_PORTS.get(protocol, 0) or 0)
    if protocol == "webdav":
        host = creds.get("url", "")
        port = 0
    return {"protocol": protocol, "host": host, "port": port,
            "username": creds.get("user", ""),
            "has_password": bool(creds.get("pass")),
            "configured": True}


def remove_nas_config():
    """删除已保存的 NAS 凭据。"""
    try:
        os.remove(_creds_path())
        logger.info("已删除 NAS 凭据。")
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# rclone 调用
# ---------------------------------------------------------------------------

def rclone_available():
    """rclone 二进制是否存在。"""
    return shutil.which("rclone") is not None


def _rclone_obscure(plaintext):
    """调用 `rclone obscure` 处理密码；rclone 缺失时中文报错，绝不回退明文。"""
    if not plaintext:
        return ""
    if not rclone_available():
        raise RuntimeError("未找到 rclone，无法处理 NAS 密码，请先安装 rclone。")
    try:
        result = subprocess.run(
            ["rclone", "obscure", plaintext],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("rclone obscure 超时。") from e
    if result.returncode != 0:
        raise RuntimeError(
            "rclone obscure 失败：%s" % (result.stderr or "").strip()[:200]
        )
    obscured = (result.stdout or "").strip()
    if not obscured:
        raise RuntimeError("rclone obscure 返回空结果。")
    _reject_control_chars("obscured 密码", obscured)
    return obscured


def _write_temp_conf(creds):
    """写临时 rclone.conf（0600，用后即删）；密码经 rclone obscure 处理。"""
    obscure = set((creds.get("_obscure_keys") or "").split(","))
    lines = ["[%s]" % REMOTE_NAME]
    for key, value in creds.items():
        if not isinstance(key, str) or key.startswith("_") or key == "type":
            continue
        value = "" if value is None else str(value)
        _reject_control_chars("配置键 %r" % key, key)
        _reject_control_chars("配置值 %r" % key, value)
        if key in obscure and value:
            value = _rclone_obscure(value)
        lines.append("%s = %s" % (key, value))
    lines.insert(1, "type = %s" % creds.get("type", ""))
    fd, path = tempfile.mkstemp(prefix="teslausb-rclone-", suffix=".conf")
    try:
        os.write(fd, "\n".join(lines).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return path


def _remove_temp_conf(path):
    try:
        if path:
            os.remove(path)
    except FileNotFoundError:
        pass


def _remote(path=""):
    return "%s:%s" % (REMOTE_NAME, path)


def test_connection():
    """测试 NAS 连接。返回 (成功, 中文消息)。"""
    creds = _load_creds()
    if not creds:
        return False, "尚未配置 NAS，请先填写连接信息。"
    if not rclone_available():
        return False, "未找到 rclone，请先安装 rclone 后再试。"
    conf = None
    try:
        conf = _write_temp_conf(creds)
        result = subprocess.run(
            ["rclone", "lsd", "--config", conf, _remote()],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return True, "连接成功。"
        err = (result.stderr or "").strip()
        return False, "连接失败：%s" % (err[:300] if err else "未知错误")
    except subprocess.TimeoutExpired:
        return False, "连接超时（30 秒）。"
    except RuntimeError as e:
        return False, str(e)
    except Exception as e:
        logger.exception("测试 NAS 连接异常")
        return False, "连接异常：%s" % e
    finally:
        _remove_temp_conf(conf)


def sync_folder(local_dir, remote_subpath, progress_cb=None, total_bytes=0,
                base_bytes=0):
    """用 `rclone sync` 把本地目录同步到 NAS 子路径。

    命令固定追加 ``--partial --inplace``（断点续传）。进度通过
    ``--use-json-log`` 解析 stats 行；解析失败则只报告起止。

    Args:
        local_dir: 本地目录。
        remote_subpath: 远端子路径，如 ``TeslaCam/RecentClips``。
        progress_cb: 回调 ``cb(percent:int, message:str)``。
        total_bytes: 本次备份全部字节数（用于换算总进度）。
        base_bytes: 本目录开始前已完成的字节数。
    Raises:
        RuntimeError: rclone 缺失或同步失败（中文信息）。
    """
    if not rclone_available():
        raise RuntimeError("未找到 rclone，无法执行备份，请先安装 rclone。")
    creds = _load_creds()
    if not creds:
        raise RuntimeError("尚未配置 NAS，请先填写连接信息。")
    conf = _write_temp_conf(creds)
    dest = _remote(remote_subpath)
    cmd = ["rclone", "sync", "--config", conf,
           "--partial", "--inplace",          # 断点续传（固定）
           "--use-json-log", "--stats", "2s", "--stats-log-level", "NOTICE",
           "--log-level", "NOTICE",
           local_dir, dest]
    logger.info("开始同步：%s -> %s", local_dir, dest)
    if progress_cb:
        progress_cb(0, "正在同步 %s…" % os.path.basename(local_dir.rstrip("/")))
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        folder_done = 0
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            stats = obj.get("stats")
            if not isinstance(stats, dict):
                continue
            done = stats.get("bytes") or 0
            total = stats.get("totalBytes") or total_bytes or 0
            folder_done = done
            if progress_cb and total > 0:
                pct = int((base_bytes + done) / total_bytes * 100) \
                    if total_bytes > 0 else 0
                progress_cb(max(0, min(99, pct)),
                            "正在同步 %s…" % os.path.basename(local_dir.rstrip("/")))
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError("同步 %s 失败（rclone 退出码 %d）。"
                               % (os.path.basename(local_dir.rstrip("/")), rc))
        logger.info("同步完成：%s", dest)
        return folder_done
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
        _remove_temp_conf(conf)
