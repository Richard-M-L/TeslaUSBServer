#!/usr/bin/env bash
# TeslaUSB-CN：edit 模式 —— 卸载 USB gadget，把镜像挂载为本地可读写分区
#
# 流程：卸载 gadget（rmmod g_mass_storage）→ losetup + mount -o rw → 写 state/mode.txt=edit
#
# 精简说明：上游 fork 的 edit_usb.sh（509 行）含 Samba 启停、configfs 手工拆除、
# 性能打点等；本版只保留核心挂载流程，Samba 由 Workstream A 另行管理。
set -euo pipefail

# ---------- 路径（与 web/services/config.py 的默认值保持一致） ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
TESLAUSB_HOME_DIR="${TESLAUSB_HOME:-/home/pi/TeslaUSB}"
STATE_DIR="${TESLAUSB_STATE_DIR:-$TESLAUSB_HOME_DIR/state}"
IMAGES_DIR="${TESLAUSB_IMAGES_DIR:-$REPO_DIR/images}"
MNT_DIR="${TESLAUSB_MNT_DIR:-/mnt/teslausb}"
IMG_CAM="$IMAGES_DIR/usb_cam.img"
IMG_LIGHTSHOW="$IMAGES_DIR/usb_lightshow.img"
MODE_FILE="$STATE_DIR/mode.txt"

echo "== TeslaUSB-CN：切换到 edit 模式（本地可读写）=="

# ---------- 1. 断开 USB gadget ----------
if lsmod | grep -q '^g_mass_storage'; then
  echo "正在断开 USB gadget..."
  sync
  sleep 1
  if ! sudo rmmod g_mass_storage 2>/dev/null; then
    echo "错误：无法移除 g_mass_storage 模块，gadget 可能仍被占用。" >&2
    exit 1
  fi
  sleep 1
  echo "  gadget 已断开"
else
  echo "  gadget 未加载，跳过断开"
fi

# ---------- 2. 分离残留的 loop 设备 ----------
echo "清理残留的 loop 设备..."
for img in "$IMG_CAM" "$IMG_LIGHTSHOW"; do
  for loop in $(sudo losetup -j "$img" 2>/dev/null | cut -d: -f1); do
    if [ -n "$loop" ]; then
      echo "  分离 $loop ..."
      sudo losetup -d "$loop" 2>/dev/null || true
    fi
  done
done

# ---------- 3. 挂载分区（读写） ----------
# 用法：mount_one <镜像> <挂载点> <描述>
mount_one() {
  local img="$1" mp="$2" desc="$3"
  if [ ! -f "$img" ]; then
    echo "错误：镜像文件不存在：$img" >&2
    return 1
  fi
  sudo mkdir -p "$mp"
  if mountpoint -q "$mp" 2>/dev/null; then
    echo "  $desc 已挂载于 $mp，跳过"
    return 0
  fi
  local loop
  loop="$(sudo losetup --show -f "$img" 2>/dev/null || true)"
  if [ -z "$loop" ]; then
    echo "错误：无法为 $desc 创建 loop 设备" >&2
    return 1
  fi
  local fstype
  fstype="$(sudo blkid -o value -s TYPE "$loop" 2>/dev/null || echo vfat)"
  echo "  挂载 $desc（$fstype，读写）到 $mp ..."
  case "$fstype" in
    exfat) sudo mount -t exfat -o rw "$loop" "$mp" ;;
    vfat)  sudo mount -t vfat -o rw,umask=000 "$loop" "$mp" ;;
    *)     sudo mount -o rw "$loop" "$mp" ;;
  esac
  if ! mountpoint -q "$mp" 2>/dev/null; then
    echo "错误：$desc 挂载失败" >&2
    sudo losetup -d "$loop" 2>/dev/null || true
    return 1
  fi
  echo "  $desc 已挂载"
}

mount_one "$IMG_CAM" "$MNT_DIR/part1" "TeslaCam"
mount_one "$IMG_LIGHTSHOW" "$MNT_DIR/part2" "LightShow"
sync

# ---------- 4. 写模式状态 ----------
mkdir -p "$STATE_DIR"
echo "edit" > "$MODE_FILE"
echo "已写入模式状态：edit（$MODE_FILE）"

echo "成功：已进入 edit 模式，本地可读写分区："
echo "  - TeslaCam：$MNT_DIR/part1"
echo "  - LightShow：$MNT_DIR/part2"
