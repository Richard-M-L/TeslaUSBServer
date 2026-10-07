"""锁车提示音服务（slim 移植版）。

移植来源：fork 仓库 ``scripts/web/services/lock_chime_service.py``（约 1300 行）。
取舍说明：
- 保留：特斯拉 WAV 格式校验（PCM / 16-bit / 44.1kHz 或 48kHz / 单声道或立体声 /
  ≤1MB）、曲库管理、音量归一化（ffmpeg loudnorm）。
- 砍掉：present/edit 双模式分支、quick_edit_part2 临时挂载、USB gadget rebind、
  与 Samba/蓝图层的耦合。本模块只做纯文件操作，可被 Web 蓝图或开机脚本直接调用。

存储布局（LightShow 分区根由 ``web.services.config.get_lightshow_root()`` 提供）：

    <root>/Chimes/<name>.wav   提示音曲库
    <root>/LockChime.wav       当前生效的锁车提示音（特斯拉读取此文件）

active 实现选择：**复制**而非符号链接/硬链接。
理由：1) LightShow 分区为 FAT32，原生不支持 symlink；
     2) 沿用上游 ``replace_lock_chime`` 的"写临时文件 + fsync + 原子 rename"做法，
        能让特斯拉侧的 USB 文件缓存失效，链接达不到同等效果。

本模块无时间逻辑，不涉及车机时区。
用户可见消息一律中文；仅依赖标准库。
"""

import contextlib
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import wave

from .config import get_lightshow_root

logger = logging.getLogger(__name__)

# 特斯拉锁车提示音硬性要求
MAX_CHIME_SIZE = 1024 * 1024  # 1 MiB
CHIMES_DIRNAME = "Chimes"          # 曲库目录名（LightShow 分区根下）
LOCK_CHIME_FILENAME = "LockChime.wav"  # 生效文件（LightShow 分区根下）

__all__ = [
    "list_chimes",
    "set_active",
    "upload_chime",
    "delete_chime",
    "validate_tesla_wav",
]


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _chimes_dir(root=None):
    """曲库目录路径（不存在时自动创建）。"""
    path = os.path.join(root or get_lightshow_root(), CHIMES_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def _safe_chime_name(name):
    """清洗提示音文件名，防路径穿越。非法时抛 ValueError（中文说明）。"""
    base = os.path.basename(str(name or "").strip())
    stem, ext = os.path.splitext(base)
    if not stem or ext.lower() != ".wav":
        raise ValueError(f"文件名不合法（需为 .wav 文件）：{name}")
    if base in (".", ".."):
        raise ValueError(f"文件名不合法：{name}")
    return base


def _md5(path):
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wav_duration_s(path):
    """读取 wav 时长（秒）。"""
    with contextlib.closing(wave.open(path, "rb")) as wav_file:
        return wav_file.getnframes() / float(wav_file.getframerate())


def _save_upload(uploaded, dest_path):
    """把上传对象落盘。兼容 Flask FileStorage（.save）、文件对象（.read）与路径。"""
    save = getattr(uploaded, "save", None)
    if callable(save):
        save(dest_path)
        return
    if hasattr(uploaded, "read"):
        with open(dest_path, "wb") as out:
            while True:
                chunk = uploaded.read(65536)
                if not chunk:
                    break
                out.write(chunk)
        return
    if isinstance(uploaded, (str, os.PathLike)) and os.path.isfile(uploaded):
        shutil.copyfile(uploaded, dest_path)
        return
    raise TypeError("不支持的上传对象类型（需要 .save/.read 方法或文件路径）")


def _uploaded_filename(uploaded, fallback=""):
    name = getattr(uploaded, "filename", None) or fallback
    if isinstance(uploaded, (str, os.PathLike)) and not name:
        name = os.path.basename(os.fspath(uploaded))
    return name or "upload.wav"


def _atomic_replace(src_path, dest_path):
    """原子替换目标文件：写临时文件 + fsync + os.replace。

    沿用上游 replace_lock_chime 的做法：特斯拉会缓存 USB 上的文件，
    同名覆盖可能读到旧内容；经临时文件 rename 能让缓存失效。
    旧文件备份为 oldLockChime.wav（上游同名行为，保留以便回滚）。
    """
    dest_dir = os.path.dirname(dest_path)
    os.makedirs(dest_dir, exist_ok=True)
    tmp_path = os.path.join(dest_dir, ".LockChime.wav.tmp")
    backup_path = os.path.join(dest_dir, "oldLockChime.wav")

    for orphan in (tmp_path,):
        if os.path.isfile(orphan):
            try:
                os.remove(orphan)
            except OSError as exc:
                logger.warning(f"清理残留临时文件失败：{exc}")

    if os.path.isfile(dest_path):
        try:
            if os.path.isfile(backup_path):
                os.remove(backup_path)
            shutil.copyfile(dest_path, backup_path)
        except OSError as exc:
            logger.warning(f"备份旧锁车提示音失败（继续执行）：{exc}")

    size = os.path.getsize(src_path)
    shutil.copyfile(src_path, tmp_path)
    if os.path.getsize(tmp_path) != size:
        raise IOError("临时文件大小校验失败")
    with open(tmp_path, "r+b") as fh:
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, dest_path)
    # 尽力保证 rename 落盘
    try:
        dir_fd = os.open(dest_dir, os.O_DIRECTORY)
    except OSError:
        dir_fd = None
    if dir_fd is not None:
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _normalize_volume(src_path, dest_path):
    """音量归一化（ffmpeg loudnorm，并统一为 44.1kHz/单声道/16-bit PCM）。

    返回：'done'（成功）/ 'skipped'（无 ffmpeg，中文日志说明，不阻塞上传）/
         'failed'（ffmpeg 出错，中文日志说明，不阻塞上传，调用方继续用原文件）。
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.info("未检测到 ffmpeg，跳过音量归一化（不影响上传）")
        return "skipped"
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", src_path,
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le",
        dest_path,
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=120, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning(f"音量归一化失败，已跳过（不影响上传）：{exc}")
        return "failed"
    ok, msg = validate_tesla_wav(dest_path)
    if not ok:
        logger.warning(f"归一化后文件不符合特斯拉要求，已跳过：{msg}")
        return "failed"
    logger.info("音量归一化完成（响度 -16 LUFS，44.1kHz/单声道/16-bit）")
    return "done"


# ---------------------------------------------------------------------------
# 对外接口（docs/INTERFACES.md）
# ---------------------------------------------------------------------------

def validate_tesla_wav(path):
    """校验 WAV 是否符合特斯拉锁车提示音要求。

    要求：≤1MB、PCM 无压缩、16-bit、44.1kHz 或 48kHz、单声道或立体声。
    返回 (是否合法, 中文说明)。
    """
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return False, f"无法读取文件：{exc}"
    if size == 0:
        return False, "文件为空。"
    if size > MAX_CHIME_SIZE:
        return False, f"文件 {size / 1024 / 1024:.2f} MB，特斯拉要求锁车提示音小于 1MB。"
    try:
        with contextlib.closing(wave.open(path, "rb")) as wav_file:
            params = wav_file.getparams()
            if params.comptype != "NONE":
                return False, "文件不是无压缩 PCM 格式。"
            if params.sampwidth != 2:
                return False, f"文件为 {params.sampwidth * 8}-bit，特斯拉要求 16-bit。"
            if params.framerate not in (44100, 48000):
                return False, f"采样率为 {params.framerate / 1000:.1f} kHz，特斯拉要求 44.1 或 48 kHz。"
            if params.nchannels not in (1, 2):
                return False, f"文件有 {params.nchannels} 个声道，特斯拉要求单声道或立体声。"
    except (wave.Error, EOFError):
        return False, "不是有效的 WAV 文件。"
    return True, "校验通过"


def list_chimes():
    """列出曲库：[{name, duration_s, is_active}]。

    is_active 按文件内容 MD5 与 LockChime.wav 比对判定
    （比上游按文件大小判定更可靠）。
    """
    root = get_lightshow_root()
    lib = _chimes_dir(root)
    active_path = os.path.join(root, LOCK_CHIME_FILENAME)
    active_md5 = _md5(active_path) if os.path.isfile(active_path) else None

    result = []
    for entry in sorted(os.listdir(lib)):
        if not entry.lower().endswith(".wav"):
            continue
        path = os.path.join(lib, entry)
        if not os.path.isfile(path):
            continue
        try:
            duration = round(_wav_duration_s(path), 2)
        except (wave.Error, EOFError, OSError):
            duration = 0.0
        try:
            is_active = active_md5 is not None and _md5(path) == active_md5
        except OSError:
            is_active = False
        result.append({"name": entry, "duration_s": duration, "is_active": is_active})
    return result


def set_active(name):
    """把曲库中的提示音设为当前锁车提示音（复制到 LockChime.wav）。

    返回 (成功与否, 中文消息)。
    """
    try:
        safe = _safe_chime_name(name)
    except ValueError as exc:
        return False, str(exc)
    lib = _chimes_dir()
    src = os.path.join(lib, safe)
    if not os.path.isfile(src):
        return False, f"提示音不存在：{safe}"
    ok, msg = validate_tesla_wav(src)
    if not ok:
        return False, f"该提示音不符合特斯拉要求，无法设为当前：{msg}"
    try:
        _atomic_replace(src, os.path.join(get_lightshow_root(), LOCK_CHIME_FILENAME))
    except OSError as exc:
        logger.error(f"设置当前提示音失败：{exc}")
        return False, f"设置失败：{exc}"
    logger.info(f"已将「{safe}」设为当前锁车提示音")
    return True, f"已将「{safe}」设为当前锁车提示音"


def upload_chime(file, normalize_volume=True):
    """上传提示音到曲库。

    校验：.wav 后缀、≤1MB、符合特斯拉 WAV 要求。
    normalize_volume=True 且系统有 ffmpeg 时做响度归一化；
    无 ffmpeg 则中文日志说明并跳过，不阻塞上传。
    同名文件会被覆盖。返回 (成功与否, 中文消息)。
    """
    try:
        safe = _safe_chime_name(_uploaded_filename(file))
    except ValueError as exc:
        return False, str(exc)

    lib = _chimes_dir()
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="chime_upload_", suffix=".wav", dir=lib)
    os.close(tmp_fd)
    try:
        try:
            _save_upload(file, tmp_path)
        except (TypeError, OSError) as exc:
            return False, f"保存上传文件失败：{exc}"

        ok, msg = validate_tesla_wav(tmp_path)
        if not ok:
            return False, f"文件不符合特斯拉锁车提示音要求：{msg}"

        final_path = tmp_path
        normalized = False
        if normalize_volume:
            norm_path = tmp_path + ".norm.wav"
            status = _normalize_volume(tmp_path, norm_path)
            if status == "done":
                os.remove(tmp_path)
                final_path = norm_path
                normalized = True
            elif os.path.isfile(norm_path):
                os.remove(norm_path)

        os.replace(final_path, os.path.join(lib, safe))
    finally:
        for leftover in (tmp_path, tmp_path + ".norm.wav"):
            if os.path.isfile(leftover):
                try:
                    os.remove(leftover)
                except OSError:
                    pass

    note = "（已做音量归一化）" if normalized else ""
    logger.info(f"已上传提示音「{safe}」{note}")
    return True, f"已上传提示音「{safe}」{note}"


def delete_chime(name):
    """从曲库删除提示音；若它正好是当前生效的，一并清除 LockChime.wav。

    返回 (成功与否, 中文消息)。
    """
    try:
        safe = _safe_chime_name(name)
    except ValueError as exc:
        return False, str(exc)
    root = get_lightshow_root()
    path = os.path.join(_chimes_dir(root), safe)
    if not os.path.isfile(path):
        return False, f"提示音不存在：{safe}"

    active_path = os.path.join(root, LOCK_CHIME_FILENAME)
    was_active = False
    if os.path.isfile(active_path):
        try:
            was_active = _md5(active_path) == _md5(path)
        except OSError:
            was_active = False

    try:
        os.remove(path)
        if was_active:
            os.remove(active_path)
    except OSError as exc:
        logger.error(f"删除提示音失败：{exc}")
        return False, f"删除失败：{exc}"

    if was_active:
        logger.info(f"已删除「{safe}」，并清除了当前生效的锁车提示音")
        return True, f"已删除「{safe}」，并清除了当前生效的锁车提示音"
    logger.info(f"已删除提示音「{safe}」")
    return True, f"已删除提示音「{safe}」"
