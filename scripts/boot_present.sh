#!/usr/bin/env bash
# TeslaUSB-CN：开机呈现 USB 存储
#
# 由 Workstream A 的 setup.sh 注册为 systemd 服务（teslausb-cn-present.service，
# ExecStart 指向本脚本），开机自动执行。
# 注意：setup.sh 当前把 teslausb-cn-present.service 的 ExecStart 直接指向
# present_usb.sh；如需“等待 UDC + 开机清理钩子”能力，请把 ExecStart 改为本脚本。
#
# 流程：
#   1. 等待 USB 控制器（UDC）就绪（最多 10 秒）
#   2. （可选）运行开机清理钩子 scripts/boot_cleanup.py
#      —— 仅当该脚本存在、且 config.yaml 中 cleanup.run_on_boot=true 时运行，
#      失败不阻塞开机（继续呈现 USB）
#   3. 调用 present_usb.sh 呈现双 LUN gadget
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
CLEANUP_HOOK="$SCRIPT_DIR/boot_cleanup.py"

echo "== TeslaUSB-CN 开机流程 =="

# ---------- 1. 等待 USB 控制器（UDC）就绪 ----------
# g_mass_storage 需要 UDC 才能绑定；dwc2 驱动的 UDC 节点可能稍晚出现。
echo "等待 USB 控制器就绪..."
UDC_READY=0
for _ in $(seq 1 100); do
  if [ -n "$(ls /sys/class/udc 2>/dev/null)" ]; then
    UDC_READY=1
    break
  fi
  sleep 0.1
done
if [ "$UDC_READY" -eq 1 ]; then
  echo "  USB 控制器已就绪：$(ls /sys/class/udc | head -n1)"
else
  echo "  警告：10 秒内未发现 UDC，仍继续尝试呈现（gadget 可能稍后绑定）"
fi

# ---------- 2. （可选）开机清理钩子 ----------
if [ -f "$CLEANUP_HOOK" ]; then
  # 读 config.yaml 的清理开关（用仓库自带的 web/config.py 解析器）
  RUN_BOOT_CLEANUP="$(python3 - "$REPO_DIR" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1] + "/web")
try:
    from config import get
    print("yes" if get("cleanup.run_on_boot", False) else "no")
except Exception:
    print("no")
EOF
)"
  if [ "$RUN_BOOT_CLEANUP" = "yes" ]; then
    echo "运行开机清理钩子..."
    if python3 "$CLEANUP_HOOK"; then
      echo "  开机清理完成"
    else
      echo "  警告：开机清理失败（退出码 $?），继续呈现 USB"
    fi
  else
    echo "跳过开机清理（cleanup.run_on_boot 未启用）"
  fi
else
  echo "跳过开机清理（无清理钩子 $CLEANUP_HOOK）"
fi

# ---------- 3. 呈现 USB gadget ----------
echo "呈现 USB 存储..."
exec "$SCRIPT_DIR/present_usb.sh"
