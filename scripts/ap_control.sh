#!/usr/bin/env bash
# TeslaUSB-CN：AP 热点控制脚本（供 Web 界面 / 命令行调用）
#
# 移植自上游 fork 的 ap_control.sh（基本原样，注释改中文）。
# 本脚本只管理热点的“期望状态”（force 模式）并上报运行状态；
# hostapd + dnsmasq 的实际拉起/停止由 Workstream A 的 wifi-monitor
# 根据 force 模式完成。
#
# 用法：ap_control.sh [status|force-on|force-off|force-auto|reload]
#   status     输出 JSON 状态
#   force-on   强制开启热点（直到再次变更）
#   force-off  强制关闭热点（阻止自动拉起）
#   force-auto 恢复自动行为（由 wifi-monitor 按需拉起）
#   reload     重新加载配置（热点运行中时重启以生效）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
TESLAUSB_HOME_DIR="${TESLAUSB_HOME:-/home/pi/TeslaUSB}"
STATE_DIR="${TESLAUSB_STATE_DIR:-$TESLAUSB_HOME_DIR/state}"
AP_CONFIG_JSON="$STATE_DIR/ap_config.json"

RUNTIME_DIR="/run/teslausb-ap"
AP_STATE_FILE="$RUNTIME_DIR/ap.state"
FORCE_MODE_FILE="$RUNTIME_DIR/force.mode"
HOSTAPD_PID="$RUNTIME_DIR/hostapd.pid"
DNSMASQ_PID="$RUNTIME_DIR/dnsmasq.pid"

# ---------- AP 配置：环境变量 > state/ap_config.json > 默认值 ----------
# Web 界面（wifi_service.set_ap_config）把用户配置写进 ap_config.json
read_ap_config() {
  local key="$1" default="$2"
  if [ -f "$AP_CONFIG_JSON" ]; then
    python3 -c "
import json, sys
try:
    print(json.load(open('$AP_CONFIG_JSON', encoding='utf-8')).get('$key') or '')
except Exception:
    print('')
" 2>/dev/null || echo "$default"
  else
    echo "$default"
  fi
}

AP_SSID="${TESLAUSB_AP_SSID:-${OFFLINE_AP_SSID:-$(read_ap_config ssid "TeslaUSB-CN")}}"
[ -z "$AP_SSID" ] && AP_SSID="TeslaUSB-CN"
AP_IPV4_CIDR="${TESLAUSB_AP_CIDR:-${OFFLINE_AP_IPV4_CIDR:-192.168.4.1/24}}"
AP_DHCP_START="${TESLAUSB_AP_DHCP_START:-${OFFLINE_AP_DHCP_START:-192.168.4.10}}"
AP_DHCP_END="${TESLAUSB_AP_DHCP_END:-${OFFLINE_AP_DHCP_END:-192.168.4.50}}"
AP_VIRTUAL_IF="${TESLAUSB_AP_IF:-${OFFLINE_AP_VIRTUAL_IF:-uap0}}"
WIFI_IF="${TESLAUSB_WIFI_IF:-${OFFLINE_AP_INTERFACE:-wlan0}}"
RETRY_SECONDS="${TESLAUSB_AP_RETRY:-${OFFLINE_AP_RETRY_SECONDS:-300}}"

ensure_runtime_dir() {
  mkdir -p "$RUNTIME_DIR"
}

# 当前 force 模式：运行时文件优先，其次环境变量，默认 auto
get_force_mode() {
  if [ -f "$FORCE_MODE_FILE" ]; then
    local mode
    mode="$(cat "$FORCE_MODE_FILE" 2>/dev/null || echo "auto")"
    case "$mode" in
      force_on|force_off|auto) echo "$mode" ;;
      *) echo "auto" ;;
    esac
  elif [ -n "${OFFLINE_AP_FORCE_MODE:-}" ]; then
    echo "${OFFLINE_AP_FORCE_MODE}"
  else
    echo "auto"
  fi
}

set_force_mode() {
  local mode="$1"
  ensure_runtime_dir
  # 写入运行时文件（立即生效，重启后失效——避免崩溃导致热点永久关闭）
  echo "$mode" > "$FORCE_MODE_FILE"
  # 唤醒 wifi-monitor 立即感知变更（best effort）
  systemctl kill -s SIGUSR1 teslausb-cn-wifi-monitor.service 2>/dev/null || \
    systemctl kill -s SIGUSR1 wifi-monitor.service 2>/dev/null || true
}

# 进程是否在运行（比状态文件更可靠）
pid_running() {
  local pidfile="$1"
  [ -f "$pidfile" ] && ps -p "$(cat "$pidfile" 2>/dev/null)" >/dev/null 2>&1
}

status_json() {
  local force hostapd_running dnsmasq_running active gateway
  force="$(get_force_mode)"
  gateway="${AP_IPV4_CIDR%%/*}"
  pid_running "$HOSTAPD_PID" && hostapd_running=true || hostapd_running=false
  pid_running "$DNSMASQ_PID" && dnsmasq_running=true || dnsmasq_running=false
  # hostapd 与 dnsmasq 都在运行才算热点激活
  if [ "$hostapd_running" = true ] && [ "$dnsmasq_running" = true ]; then
    active=true
  else
    active=false
  fi
  cat <<EOF
{
  "ap_active": $active,
  "force_mode": "$force",
  "ap_interface": "$AP_VIRTUAL_IF",
  "wifi_interface": "$WIFI_IF",
  "static_ip": "$gateway",
  "dhcp_range_start": "$AP_DHCP_START",
  "dhcp_range_end": "$AP_DHCP_END",
  "ssid": "$AP_SSID",
  "retry_seconds": "$RETRY_SECONDS",
  "hostapd_pid": "$([ -f "$HOSTAPD_PID" ] && cat "$HOSTAPD_PID")",
  "dnsmasq_pid": "$([ -f "$DNSMASQ_PID" ] && cat "$DNSMASQ_PID")"
}
EOF
}

usage() {
  cat <<EOF
用法：$0 [status|force-on|force-off|force-auto|reload]
  status      输出 JSON 状态
  force-on    强制开启热点（直到再次变更）
  force-off   强制关闭热点（阻止自动拉起）
  force-auto  恢复自动行为
  reload      重新加载配置（热点运行中时重启以生效）
EOF
}

reload_ap() {
  # 先强制关闭以干净地停掉热点，避免竞态
  set_force_mode "force_off"
  sleep 2
  # 清掉旧的 hostapd/dnsmasq 配置，下次拉起时按新配置重新生成
  rm -f "$RUNTIME_DIR/hostapd.conf" "$RUNTIME_DIR/dnsmasq.conf"
  # 回到自动模式：wifi-monitor 会按需重新拉起热点
  set_force_mode "auto"
}

case "${1-}" in
  status)    status_json ;;
  force-on)  set_force_mode "force_on" ;;
  force-off) set_force_mode "force_off" ;;
  force-auto) set_force_mode "auto" ;;
  reload)    reload_ap ;;
  *)         usage; exit 1 ;;
esac
