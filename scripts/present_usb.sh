#!/usr/bin/env bash
# TeslaUSB-CN：present 模式 —— 把两个镜像文件作为 USB 大容量存储呈现给特斯拉
#
# 流程：卸载本地分区 → modprobe g_mass_storage（双 LUN）→ 写 state/mode.txt=present
#   LUN0 = TeslaCam 镜像（exFAT，读写：特斯拉行车记录 / 哨兵写入）
#   LUN1 = LightShow 镜像（FAT32，只读：灯光秀 / 锁车提示音读取更快更稳）
#
# 精简说明：上游 fork 的 present_usb.sh（583 行）含 Samba 启停、configfs 手工组装、
# 性能打点、analytics 遥测上报等；本版只保留核心 gadget 流程，Samba/遥测已删除。
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

echo "== TeslaUSB-CN：切换到 present 模式（USB 存储呈现）=="

# ---------- 1. 检查镜像文件是否存在 ----------
for img in "$IMG_CAM" "$IMG_LIGHTSHOW"; do
  if [ ! -f "$img" ]; then
    echo "错误：镜像文件不存在：$img" >&2
    echo "请先运行 setup.sh 创建镜像。" >&2
    exit 1
  fi
done

# ---------- 2. 落盘并卸载本地分区 ----------
echo "正在落盘并卸载本地分区..."
sync
for mp in "$MNT_DIR/part1" "$MNT_DIR/part2"; do
  if mountpoint -q "$mp" 2>/dev/null; then
    echo "  卸载 $mp ..."
    # 最多重试 3 次；忙则终止占用进程；最后手段用 lazy 卸载
    for i in 1 2 3; do
      if sudo umount "$mp" 2>/dev/null; then
        break
      fi
      echo "  $mp 忙（第 $i 次），尝试终止占用进程..."
      sudo fuser -km "$mp" 2>/dev/null || true
      sleep 1
    done
    if mountpoint -q "$mp" 2>/dev/null; then
      echo "  强制 lazy 卸载 $mp ..."
      sudo umount -lf "$mp" 2>/dev/null || true
      sleep 1
    fi
    if mountpoint -q "$mp" 2>/dev/null; then
      echo "错误：无法卸载 $mp，为防止损坏镜像而中止。" >&2
      exit 1
    fi
    echo "  已卸载 $mp"
  fi
done
sync

# ---------- 3. 分离占用镜像的残留 loop 设备 ----------
echo "清理残留的 loop 设备..."
for img in "$IMG_CAM" "$IMG_LIGHTSHOW"; do
  for loop in $(sudo losetup -j "$img" 2>/dev/null | cut -d: -f1); do
    if [ -n "$loop" ]; then
      echo "  分离 $loop ..."
      sudo losetup -d "$loop" 2>/dev/null || true
    fi
  done
done

# ---------- 4. 移除旧的 gadget 模块（如有） ----------
if lsmod | grep -q '^g_mass_storage'; then
  echo "移除旧的 g_mass_storage 模块..."
  sudo rmmod g_mass_storage 2>/dev/null || true
  sleep 1
fi

# ---------- 5. 加载 g_mass_storage：双 LUN ----------
# ro=0,1：LUN0 读写（特斯拉录像写入），LUN1 只读（灯光秀/提示音读取）
echo "加载 USB gadget（g_mass_storage，双 LUN）..."
sudo modprobe g_mass_storage \
  file="$IMG_CAM,$IMG_LIGHTSHOW" \
  ro=0,1 removable=1 stall=0 \
  idVendor=0x1d6b idProduct=0x0104 \
  iManufacturer="TeslaUSB-CN" iProduct="Tesla Storage"

# ---------- 6. 写模式状态 ----------
mkdir -p "$STATE_DIR"
echo "present" > "$MODE_FILE"
echo "已写入模式状态：present（$MODE_FILE）"

echo "成功：USB 存储已呈现"
echo "  - LUN0：TeslaCam（读写）——特斯拉可录制行车记录 / 哨兵"
echo "  - LUN1：LightShow（只读）——灯光秀 / 锁车提示音"
