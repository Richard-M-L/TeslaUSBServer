"""灯光秀服务（按接口自写，参考 fork 的 light_show_service.py）。

取舍说明：
- fork 的 light_show_service 只做 LightShow 分区根目录的扁平文件上传/删除，
  且与 present/edit 双模式、quick_edit 临时挂载强耦合。
- 本项目按 docs/INTERFACES.md 接口实现：LightShow 分区下按灯光秀名称建目录，
  每个目录内 FSEQ + MP3 配对存放（特斯拉灯光秀要求 .fseq 灯光序列与同名音频
  文件配对，音频可用 MP3）。
- 砍掉：ZIP 上传、双模式分支、与分区挂载服务的耦合；只保留纯文件操作。

存储布局（LightShow 分区根由 ``web.services.config.get_lightshow_root()`` 提供）：

    <root>/<show_name>/<show_name>.fseq
    <root>/<show_name>/<show_name>.mp3

用户可见消息一律中文；仅依赖标准库。
"""

import logging
import os
import shutil
import tempfile

from .config import get_lightshow_root

logger = logging.getLogger(__name__)

__all__ = ["list_shows", "upload_show", "delete_show"]


def _safe_show_name(name):
    """清洗灯光秀名称，防路径穿越。非法时抛 ValueError（中文说明）。"""
    base = os.path.basename(str(name or "").strip())
    if not base or base in (".", ".."):
        raise ValueError(f"灯光秀名称不合法：{name}")
    if len(base) > 64:
        raise ValueError(f"灯光秀名称过长（≤64 字符）：{name}")
    return base


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


def _uploaded_stem(uploaded, fallback=""):
    name = getattr(uploaded, "filename", None) or fallback
    if isinstance(uploaded, (str, os.PathLike)) and not name:
        name = os.path.basename(os.fspath(uploaded))
    return os.path.splitext(os.path.basename(name or ""))[0]


def list_shows():
    """列出灯光秀：[{name, has_fseq, has_mp3}]。"""
    root = get_lightshow_root()
    result = []
    if not os.path.isdir(root):
        return result
    for entry in sorted(os.listdir(root)):
        show_dir = os.path.join(root, entry)
        if not os.path.isdir(show_dir):
            continue
        try:
            files = [f.lower() for f in os.listdir(show_dir)]
        except OSError:
            continue
        result.append({
            "name": entry,
            "has_fseq": any(f.endswith(".fseq") for f in files),
            "has_mp3": any(f.endswith(".mp3") for f in files),
        })
    return result


def upload_show(fseq_file, mp3_file):
    """上传一组灯光秀文件（FSEQ + MP3，需同名配对）。

    以 fseq 文件名（去后缀）为灯光秀名称，在 LightShow 分区根下建目录存放。
    同名灯光秀已存在时覆盖。返回 (成功与否, 中文消息)。
    """
    name = _safe_show_name(_uploaded_stem(fseq_file, "show"))
    mp3_stem = _uploaded_stem(mp3_file, "")
    if mp3_stem != name:
        return False, f"FSEQ 与 MP3 文件名不一致（{name} ≠ {mp3_stem}），需同名配对"

    root = get_lightshow_root()
    show_dir = os.path.join(root, name)
    os.makedirs(show_dir, exist_ok=True)

    staged = []
    try:
        for uploaded, ext in ((fseq_file, ".fseq"), (mp3_file, ".mp3")):
            fd, tmp = tempfile.mkstemp(prefix="show_upload_", suffix=ext, dir=show_dir)
            os.close(fd)
            staged.append(tmp)
            try:
                _save_upload(uploaded, tmp)
            except (TypeError, OSError) as exc:
                return False, f"保存上传文件失败：{exc}"
            if os.path.getsize(tmp) == 0:
                return False, f"上传的 {ext} 文件为空"
        os.replace(staged[0], os.path.join(show_dir, name + ".fseq"))
        os.replace(staged[1], os.path.join(show_dir, name + ".mp3"))
    finally:
        for tmp in staged:
            if os.path.isfile(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    logger.info(f"已上传灯光秀「{name}」（FSEQ + MP3 配对）")
    return True, f"灯光秀「{name}」上传成功"


def delete_show(name):
    """删除整个灯光秀目录。返回 (成功与否, 中文消息)。"""
    try:
        safe = _safe_show_name(name)
    except ValueError as exc:
        return False, str(exc)
    show_dir = os.path.join(get_lightshow_root(), safe)
    if not os.path.isdir(show_dir):
        return False, f"灯光秀不存在：{safe}"
    try:
        shutil.rmtree(show_dir)
    except OSError as exc:
        logger.error(f"删除灯光秀失败：{exc}")
        return False, f"删除失败：{exc}"
    logger.info(f"已删除灯光秀「{safe}」")
    return True, f"灯光秀「{safe}」删除成功"
