#!/usr/bin/env bash
#
# TeslaUSB-CN 精简版安装脚本
# 为国行特斯拉适配：国内镜像源、树莓派型号检测、Asia/Shanghai 时区、能耗优化
#
# 用法：
#   sudo ./setup.sh                  # 交互式安装（推荐）
#   sudo ./setup.sh --china-mirror   # 使用国内镜像源（交互选择三选一）
#   sudo ./setup.sh --mirror=tsinghua # 非交互：指定镜像源（tsinghua/ustc/aliyun）
#   sudo ./setup.sh --no-mirror      # 不换源，使用官方源
#   sudo ./setup.sh --yes            # 全接受默认值
#   ./setup.sh --help                # 帮助
#
# 幂等设计：可重复运行，已完成的步骤会自动跳过。
#
set -euo pipefail

# ================= 可调配置 =================
RCLONE_VERSION="1.69.1"      # rclone 备用 pin 版本；优先从国内镜像探测最新版
WEB_PORT=5000                # Web 界面端口
# ===========================================

# ---------- 基础路径 ----------
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_YAML="$REPO_DIR/config.yaml"
IMAGES_DIR="$REPO_DIR/images"
MNT_DIR="/mnt/teslausb-cn"
IMG_CAM="$IMAGES_DIR/usb_cam.img"
IMG_LIGHTSHOW="$IMAGES_DIR/usb_lightshow.img"
IMG_MUSIC="$IMAGES_DIR/usb_music.img"

# ---------- 全局状态 ----------
PI_MODEL="unknown"     # zero2w / pi4 / pi5 / unknown
PI_MODEL_DESC="未知"
MIRROR_CHOICE="off"    # tsinghua / ustc / aliyun / off
USE_CHINA_MIRROR=0
ASSUME_YES=0
TARGET_USER=""

# ---------- 输出函数（全部中文） ----------
log_info() { echo -e "\033[1;34m[信息]\033[0m $*"; }
log_ok()   { echo -e "\033[1;32m[成功]\033[0m $*"; }
log_warn() { echo -e "\033[1;33m[警告]\033[0m $*"; }
log_err()  { echo -e "\033[1;31m[错误]\033[0m $*" >&2; }
die()      { log_err "$*"; exit 1; }

# ---------- 简易 YAML 读取（config.yaml 格式简单，无需 pyyaml） ----------
# 用法：yaml_get <section> <key>；section 为空表示顶层键
yaml_get() {
  local section="$1" key="$2" cur="" line val matched
  while IFS= read -r line; do
    if [[ "$line" =~ ^[^[:space:]#] ]]; then
      cur="${line%%:*}"
    fi
    matched=0
    if [ -n "$section" ]; then
      [ "$cur" = "$section" ] || continue
      [[ "$line" =~ ^[[:space:]]+${key}:[[:space:]]*(.*)$ ]] || continue
      matched=1
    else
      [[ "$line" =~ ^${key}:[[:space:]]*(.*)$ ]] || continue
      matched=1
    fi
    if [ "$matched" = "1" ]; then
      val="${BASH_REMATCH[1]}"
      # 去行内注释（空格/# 或制表符+# 之后的内容；值内如需 # 请勿加空格）
      val="$(printf '%s' "$val" | sed -E 's/[[:space:]]+#.*$//')"
      # 去首尾空白
      val="$(printf '%s' "$val" | sed -E 's/^[[:space:]]+//;s/[[:space:]]+$//')"
      # 去首尾引号
      if [[ "$val" =~ ^\"(.*)\"$ ]]; then val="${BASH_REMATCH[1]}"; fi
      if [[ "$val" =~ ^\'(.*)\'$ ]]; then val="${BASH_REMATCH[1]}"; fi
      printf '%s' "$val"
      return 0
    fi
  done < "$CONFIG_YAML"
  return 1
}

yaml_get_default() {
  yaml_get "$1" "$2" 2>/dev/null || printf '%s' "$3"
}

# ---------- 交互询问 ----------
ask() { # $1=提示语 $2=默认值
  local prompt="$1" default="$2" answer
  if [ "$ASSUME_YES" = "1" ]; then
    printf '%s' "$default"
    return 0
  fi
  read -r -p "$prompt [$default]: " answer
  printf '%s' "${answer:-$default}"
}

# ---------- 参数解析 ----------
print_help() {
  cat <<'EOF'
TeslaUSB-CN 安装脚本

用法：
  sudo ./setup.sh                    交互式安装（推荐）
  sudo ./setup.sh --china-mirror     使用国内镜像源（交互三选一）
  sudo ./setup.sh --mirror=tsinghua  非交互指定镜像源：tsinghua/ustc/aliyun
  sudo ./setup.sh --no-mirror        不换源，使用官方源
  sudo ./setup.sh --yes              全接受默认值（配合以上参数）
  ./setup.sh --help                  显示本帮助

说明：
  - 必须用 root 或 sudo 运行
  - 幂等设计，可重复运行
  - 国内镜像源：apt（三选一）+ pip + rclone 二进制 + GitHub→gitee fallback
EOF
}

MIRROR_FLAG=""
for arg in "$@"; do
  case "$arg" in
    -h|--help) print_help; exit 0 ;;
    --china-mirror) USE_CHINA_MIRROR=1 ;;
    --no-mirror) MIRROR_FLAG="off" ;;
    --mirror=*) MIRROR_FLAG="${arg#*=}"; USE_CHINA_MIRROR=1 ;;
    --yes) ASSUME_YES=1 ;;
    *) die "未知参数：$arg（用 --help 查看用法）" ;;
  esac
done

# ---------- root 检查 ----------
if [ "$(id -u)" -ne 0 ]; then
  die "请用 root 或 sudo 运行：sudo ./setup.sh"
fi

# ---------- 目标用户 ----------
if [ -n "${SUDO_USER:-}" ]; then
  TARGET_USER="$SUDO_USER"
else
  TARGET_USER="pi"
fi
id "$TARGET_USER" >/dev/null 2>&1 || die "用户 $TARGET_USER 不存在"
log_info "目标用户：$TARGET_USER"

# ---------- 树莓派型号检测（P0：Pi 4/5 需要 otg_mode=1） ----------
detect_pi_model() {
  local model="未知"
  if [ -r /proc/device-tree/model ]; then
    model="$(tr -d '\0' < /proc/device-tree/model)"
  fi
  case "$model" in
    *"Raspberry Pi Zero 2"*) PI_MODEL="zero2w" ;;
    *"Raspberry Pi 5"*)      PI_MODEL="pi5" ;;
    *"Raspberry Pi 4"*)      PI_MODEL="pi4" ;;
    *)                      PI_MODEL="unknown" ;;
  esac
  PI_MODEL_DESC="$model"
  log_info "检测到设备：$model"
  case "$PI_MODEL" in
    zero2w)
      log_ok "Zero 2W：推荐机型。功耗约 1~2W，特斯拉 USB 口直供无忧，发热最低。"
      ;;
    pi4)
      log_warn "Pi 4：功耗较高（峰值约 8W），请确认特斯拉 USB 口供电稳定；已为 USB-C device mode 启用 otg_mode=1。"
      ;;
    pi5)
      log_warn "Pi 5：功耗高（官方电源 27W），强烈建议外接供电而非车机 USB 口；手套箱密闭环境注意散热。已为 USB-C device mode 启用 otg_mode=1。"
      ;;
    *)
      log_warn "未识别具体型号，按通用配置继续。如为 Pi 4/5，请留意 USB-C device mode 是否生效。"
      ;;
  esac
}

# ---------- 大小换算 ----------
to_mib() { # "400G"/"20G"/"512M"/"0" → MiB 数字
  local s="$1"
  if [[ "$s" == "0" ]]; then echo 0; return 0; fi
  if [[ "$s" =~ ^([0-9]+)[Gg]$ ]]; then echo $(( BASH_REMATCH[1] * 1024 )); return 0; fi
  if [[ "$s" =~ ^([0-9]+)[Mm]$ ]]; then echo "${BASH_REMATCH[1]}"; return 0; fi
  die "无法解析的分区大小：$s（应为 400G / 20G / 512M / 0）"
}

# ---------- config.txt 幂等写入 ----------
ensure_config_txt() { # $1="key=value" 行，确保在 [all] 下存在且唯一
  local line="$1" cfg="/boot/firmware/config.txt"
  [ -f "$cfg" ] || cfg="/boot/config.txt"
  if [ ! -f "$cfg" ]; then
    log_warn "未找到 config.txt（$cfg），跳过：$line"
    return 0
  fi
  if grep -qxF "$line" "$cfg" 2>/dev/null; then
    return 0
  fi
  # 删除同键旧行（避免重复/冲突）
  local key="${line%%=*}"
  sed -i "/^${key}=.*/d" "$cfg"
  if grep -q '^\[all\]' "$cfg"; then
    sed -i "/^\\[all\\]/a $line" "$cfg"
  else
    printf '\n[all]\n%s\n' "$line" >> "$cfg"
  fi
  log_info "config.txt 已写入：$line"
}

# ================= 镜像源 =================
# apt 源地址（三选一）
apt_debian_base() {
  case "$MIRROR_CHOICE" in
    tsinghua) echo "https://mirrors.tuna.tsinghua.edu.cn/debian/" ;;
    ustc)     echo "https://mirrors.ustc.edu.cn/debian/" ;;
    aliyun)   echo "https://mirrors.aliyun.com/debian/" ;;
  esac
}
apt_security_base() {
  case "$MIRROR_CHOICE" in
    tsinghua) echo "https://mirrors.tuna.tsinghua.edu.cn/debian-security/" ;;
    ustc)     echo "https://mirrors.ustc.edu.cn/debian-security/" ;;
    aliyun)   echo "https://mirrors.aliyun.com/debian-security/" ;;
  esac
}
apt_raspbian_base() {
  case "$MIRROR_CHOICE" in
    tsinghua) echo "https://mirrors.tuna.tsinghua.edu.cn/raspbian/raspbian/" ;;
    ustc)     echo "https://mirrors.ustc.edu.cn/raspbian/raspbian/" ;;
    aliyun)   echo "https://mirrors.aliyun.com/raspbian/raspbian/" ;;
  esac
}
pip_index_url() {
  case "$MIRROR_CHOICE" in
    tsinghua) echo "https://pypi.tuna.tsinghua.edu.cn/simple" ;;
    ustc)     echo "https://mirrors.ustc.edu.cn/pypi/simple" ;;
    aliyun)   echo "https://mirrors.aliyun.com/pypi/simple" ;;
  esac
}
mirror_display_name() {
  case "$MIRROR_CHOICE" in
    tsinghua) echo "清华" ;;
    ustc)     echo "中科大" ;;
    aliyun)   echo "阿里云" ;;
    *)        echo "官方源" ;;
  esac
}

choose_mirror() {
  if [ -n "$MIRROR_FLAG" ]; then
    case "$MIRROR_FLAG" in
      tsinghua|ustc|aliyun) MIRROR_CHOICE="$MIRROR_FLAG"; USE_CHINA_MIRROR=1 ;;
      off) MIRROR_CHOICE="off"; USE_CHINA_MIRROR=0 ;;
      *) die "未知镜像源：$MIRROR_FLAG（可选 tsinghua/ustc/aliyun/off）" ;;
    esac
    return 0
  fi
  if [ "$USE_CHINA_MIRROR" = "1" ]; then
    choose_mirror_interactive
    return 0
  fi
  # 无参数：交互询问
  local ans
  ans="$(ask "是否使用国内镜像源加速安装？(y/n)" "y")"
  if [[ "$ans" =~ ^[yY]$ ]]; then
    USE_CHINA_MIRROR=1
    choose_mirror_interactive
  else
    MIRROR_CHOICE="off"
    log_info "使用官方源安装（国内可能较慢）"
  fi
}

choose_mirror_interactive() {
  echo "请选择国内镜像源："
  echo "  1) 清华（默认）"
  echo "  2) 中科大"
  echo "  3) 阿里云"
  local c
  c="$(ask "请输入编号" "1")"
  case "$c" in
    1) MIRROR_CHOICE="tsinghua" ;;
    2) MIRROR_CHOICE="ustc" ;;
    3) MIRROR_CHOICE="aliyun" ;;
    *) log_warn "输入无效，使用默认：清华"; MIRROR_CHOICE="tsinghua" ;;
  esac
}

apply_apt_mirror() {
  [ "$USE_CHINA_MIRROR" = "1" ] || return 0
  log_info "切换 apt 源为$(mirror_display_name)镜像..."
  local codename
  codename="$(grep '^VERSION_CODENAME=' /etc/os-release | cut -d= -f2)"
  [ -n "$codename" ] || codename="trixie"
  local deb_base sec_base rasp_base
  deb_base="$(apt_debian_base)"
  sec_base="$(apt_security_base)"
  rasp_base="$(apt_raspbian_base)"

  # 备份现有源配置
  local bak="/etc/apt/sources.list.d/teslausb-cn.bak.$(date +%s)"
  mkdir -p "$bak"
  cp -f /etc/apt/sources.list "$bak/" 2>/dev/null || true
  for f in /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
    [ -f "$f" ] && cp -f "$f" "$bak/" 2>/dev/null || true
  done
  log_info "原 apt 源已备份到 $bak"

  # 1) 禁用现有官方源（避免重复/冲突）
  # classic .list 格式：注释掉 debian/raspbian 相关行（用 case 而不用 sed，避免分隔符转义问题）
  local f line
  for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
    [ -f "$f" ] || continue
    case "$f" in
      *debian-cn.list|*raspi-cn.list) continue ;;
    esac
    local tmpf
    tmpf="$(mktemp)"
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        deb\ *debian.org*|deb\ *raspbian*|deb-src\ *debian.org*|deb-src\ *raspbian*)
          printf '#%s  # teslausb-cn disabled, use CN mirror\n' "$line" >> "$tmpf" ;;
        *) printf '%s\n' "$line" >> "$tmpf" ;;
      esac
    done < "$f"
    cat "$tmpf" > "$f"
    rm -f "$tmpf"
  done
  # DEB822 .sources 格式：加 Enabled: no
  for f in /etc/apt/sources.list.d/*.sources; do
    [ -f "$f" ] || continue
    if grep -qE "debian\.org|raspbian" "$f" && ! grep -q "^Enabled:" "$f"; then
      sed -i 's/^Types:/Enabled: no\nTypes:/' "$f"
      log_info "已禁用 $f"
    fi
  done

  # 2) 写入干净的新源（含 non-free-firmware，保证 WiFi 固件可用）
  cat > /etc/apt/sources.list.d/debian-cn.list <<EOF
# TeslaUSB-CN：$(mirror_display_name) Debian 镜像源
deb ${deb_base} ${codename} main contrib non-free non-free-firmware
deb ${deb_base} ${codename}-updates main contrib non-free non-free-firmware
deb ${sec_base} ${codename}-security main contrib non-free non-free-firmware
EOF
  cat > /etc/apt/sources.list.d/raspi-cn.list <<EOF
# TeslaUSB-CN：$(mirror_display_name) raspbian 镜像源
deb ${rasp_base} ${codename} main contrib non-free non-free-firmware rpi
EOF
  log_ok "apt 源已切换为$(mirror_display_name)镜像"
}

apply_pip_mirror() {
  [ "$USE_CHINA_MIRROR" = "1" ] || return 0
  local url host tmp
  url="$(pip_index_url)"
  tmp="${url#https://}"; tmp="${tmp#http://}"; host="${tmp%%/*}"
  log_info "配置 pip 镜像源：$url"
  mkdir -p "/home/$TARGET_USER/.config/pip"
  cat > "/home/$TARGET_USER/.config/pip/pip.conf" <<EOF
[global]
index-url = $url
trusted-host = $host
EOF
  chown -R "$TARGET_USER:$TARGET_USER" "/home/$TARGET_USER/.config"
  log_ok "pip 镜像源已配置"
}

# GitHub → gitee fallback（写给后续脚本/升级流程用）
github_mirror_url() { # $1=GitHub URL → 输出镜像 URL（未换源则原样返回）
  local url="$1"
  if [ "$USE_CHINA_MIRROR" = "1" ] && [[ "$url" == https://github.com/* ]]; then
    printf 'https://gitee.com/mirrors/%s\n' "${url#https://github.com/}"
  else
    printf '%s\n' "$url"
  fi
}

write_mirror_to_config() {
  local tmpf found=0 line
  tmpf="$(mktemp)"
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$found" = "0" ] && [[ "$line" =~ ^mirror: ]]; then
      printf 'mirror: "%s"\n' "$MIRROR_CHOICE" >> "$tmpf"
      found=1
    else
      printf '%s\n' "$line" >> "$tmpf"
    fi
  done < "$CONFIG_YAML"
  if [ "$found" = "1" ]; then
    cat "$tmpf" > "$CONFIG_YAML"
    log_ok "已将镜像源选择（$MIRROR_CHOICE）写回 config.yaml"
  else
    log_warn "config.yaml 中未找到 mirror 键，跳过写回"
  fi
  rm -f "$tmpf"
}

# ================= 依赖安装（精简列表） =================
# 说明：去掉上游的 watchdog（新版用软件温度熔断替代）；
#       无线工具只保留 iw（nmcli 扫描）与 wireless-tools（iwconfig 兼容）
install_packages() {
  local pkgs=(
    parted
    dosfstools
    exfatprogs
    util-linux
    psmisc
    python3-flask
    samba
    samba-common-bin
    ffmpeg
    rsync
    chrony
    iw
    wireless-tools
  )
  log_info "更新软件包索引..."
  apt-get update
  local missing=()
  local p
  for p in "${pkgs[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
  done
  if [ "${#missing[@]}" -eq 0 ]; then
    log_ok "依赖已全部安装，跳过"
    return 0
  fi
  log_info "安装缺失依赖：${missing[*]}（低内存设备逐个安装）"
  for p in "${missing[@]}"; do
    log_info "正在安装 $p ..."
    apt-get install -y "$p" || apt-get install -y --no-install-recommends "$p" || {
      log_warn "$p 安装失败，继续（可能需要手动处理）"
    }
  done
  log_ok "依赖安装完成"
}

# ================= rclone（二进制，国内镜像优先） =================
install_rclone() {
  if command -v rclone >/dev/null 2>&1; then
    log_ok "rclone 已安装（$(rclone version --check 2>/dev/null | head -1 || echo 已知版本)），跳过"
    return 0
  fi
  local arch
  case "$(uname -m)" in
    aarch64) arch="arm64" ;;
    armv7l|armv6l) arch="arm" ;;
    x86_64) arch="amd64" ;;
    *) die "不支持的架构：$(uname -m)" ;;
  esac
  local ver="$RCLONE_VERSION"
  # 优先从国内镜像探测最新版本（失败则用 pin 版本）
  if [ "$USE_CHINA_MIRROR" = "1" ]; then
    local latest
    latest="$(curl -fsSL --max-time 15 "https://mirrors.tuna.tsinghua.edu.cn/rclone/" \
      | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+/' | tr -d '/' | sort -V | tail -1 || true)"
    if [ -n "$latest" ]; then
      ver="$latest"
      log_info "从镜像探测到 rclone 最新版：$ver"
    fi
  fi
  local tmpd
  tmpd="$(mktemp -d)"
  trap 'rm -rf "$tmpd"' RETURN
  local urls=()
  if [ "$USE_CHINA_MIRROR" = "1" ]; then
    urls+=(
      "https://mirrors.tuna.tsinghua.edu.cn/rclone/${ver}/rclone-${ver}-linux-${arch}.zip"
      "https://mirrors.ustc.edu.cn/rclone/${ver}/rclone-${ver}-linux-${arch}.zip"
    )
  fi
  local ok=0 u
  for u in "${urls[@]}"; do
    log_info "尝试从国内镜像下载 rclone：$u"
    if curl -fSL --max-time 120 -o "$tmpd/rclone.zip" "$u"; then
      ok=1
      break
    fi
    log_warn "下载失败，尝试下一个源..."
  done
  if [ "$ok" = "1" ]; then
    unzip -q -o "$tmpd/rclone.zip" -d "$tmpd"
    install -m 0755 "$tmpd/rclone-${ver}-linux-${arch}/rclone" /usr/local/bin/rclone
    log_ok "rclone $ver 已安装到 /usr/local/bin/rclone"
    return 0
  fi
  # 兜底：官方安装脚本
  log_warn "国内镜像下载失败，尝试官方安装脚本（rclone.org）..."
  if curl -fsSL --max-time 120 https://rclone.org/install.sh -o "$tmpd/install.sh"; then
    bash "$tmpd/install.sh" || die "rclone 官方脚本安装失败"
    log_ok "rclone 已通过官方脚本安装"
    return 0
  fi
  die "rclone 安装失败。请手动安装后重跑：https://rclone.org/install/"
}

# ================= 时区与 locale =================
setup_timezone_locale() {
  log_info "设置时区为 Asia/Shanghai..."
  if timedatectl set-timezone Asia/Shanghai 2>/dev/null; then
    log_ok "时区已设为 $(timedatectl show --property=Timezone --value 2>/dev/null || echo Asia/Shanghai)"
  else
    log_warn "timedatectl 不可用，尝试直接写 /etc/timezone"
    echo "Asia/Shanghai" > /etc/timezone 2>/dev/null || true
    ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime 2>/dev/null || true
  fi
  if locale -a 2>/dev/null | grep -qi "zh_CN.utf8"; then
    log_ok "zh_CN.UTF-8 已存在"
  elif command -v locale-gen >/dev/null 2>&1; then
    log_info "生成 zh_CN.UTF-8 ..."
    locale-gen zh_CN.UTF-8 2>/dev/null || log_warn "locale-gen 失败，可手动执行：sudo locale-gen zh_CN.UTF-8"
  else
    log_warn "未找到 locale-gen，跳过中文 locale 生成"
  fi
  if command -v update-locale >/dev/null 2>&1; then
    update-locale LANG=zh_CN.UTF-8 LC_ALL=zh_CN.UTF-8 2>/dev/null || true
    log_ok "默认 locale 已设为 zh_CN.UTF-8"
  fi
}

# ================= chrony 时间同步（替代 sntp/timesyncd） =================
setup_chrony() {
  log_info "配置 chrony 时间同步（国内 NTP）..."
  mkdir -p /etc/chrony/conf.d
  cat > /etc/chrony/conf.d/teslausb-cn.conf <<'EOF'
# TeslaUSB-CN：国内 NTP 服务器
server ntp.aliyun.com iburst
server ntp.tencent.com iburst
server time1.cloud.tencent.com iburst
EOF
  # 停用 systemd-timesyncd，避免冲突
  if systemctl list-unit-files systemd-timesyncd.service >/dev/null 2>&1; then
    systemctl disable --now systemd-timesyncd.service 2>/dev/null || true
    log_info "已停用 systemd-timesyncd"
  fi
  systemctl enable --now chrony.service 2>/dev/null || systemctl enable --now chronyd.service 2>/dev/null || \
    log_warn "chrony 服务启动失败，请手动检查"
  log_ok "chrony 已配置并启动"
}

# ================= config.txt 与内核模块 =================
setup_boot_config() {
  log_info "配置启动参数（config.txt）..."
  ensure_config_txt "dtoverlay=dwc2"
  if [ "$PI_MODEL" = "pi4" ] || [ "$PI_MODEL" = "pi5" ]; then
    # P0：Pi 4/5 的 USB-C device mode 必须开启 otg_mode=1，否则 xhci 接管，gadget 起不来
    ensure_config_txt "otg_mode=1"
  fi
  ensure_config_txt "dtparam=watchdog=on"
  ensure_config_txt "gpu_mem=16"

  local disable_led disable_bt
  disable_led="$(yaml_get_default power disable_led true)"
  disable_bt="$(yaml_get_default power disable_bluetooth true)"
  if [[ "$disable_led" =~ ^[Tt]rue$ ]]; then
    ensure_config_txt "dtparam=act_led_trigger=none"
    ensure_config_txt "dtparam=act_led_activelow=off"
    ensure_config_txt "dtparam=pwr_led_trigger=none"
    ensure_config_txt "dtparam=pwr_led_activelow=off"
    log_info "已关闭板载 LED（省电）"
  fi
  if [[ "$disable_bt" =~ ^[Tt]rue$ ]]; then
    ensure_config_txt "dtoverlay=disable-bt"
    if systemctl list-unit-files hciuart.service >/dev/null 2>&1; then
      systemctl disable --now hciuart.service 2>/dev/null || true
    fi
    log_info "已关闭蓝牙（省电）"
  fi

  # 开机加载 USB gadget 内核模块
  local modconf="/etc/modules-load.d/dwc2.conf"
  if [ ! -f "$modconf" ]; then
    cat > "$modconf" <<'EOF'
# TeslaUSB-CN：USB gadget 内核模块
dwc2
libcomposite
EOF
    log_ok "已创建 $modconf"
  else
    log_info "$modconf 已存在，跳过"
  fi
}

# ================= 能耗优化（开机应用，config.yaml 的 power 节可覆盖） =================
setup_power_service() {
  log_info "安装能耗优化服务（teslausb-cn-power）..."
  local governor max_freq wifi_ps_off disable_hdmi
  governor="$(yaml_get_default power cpu_governor ondemand)"
  max_freq="$(yaml_get_default power max_freq_mhz 1000)"
  wifi_ps_off="$(yaml_get_default power wifi_powersave_off true)"
  disable_hdmi="$(yaml_get_default power disable_hdmi true)"

  # 构建开机执行命令（config.txt 管静态项，这里管运行时项）
  local cmds=""
  cmds+="for d in /sys/devices/system/cpu/cpufreq/policy*; do [ -w \"\$d/scaling_governor\" ] && echo $governor > \"\$d/scaling_governor\" 2>/dev/null || true; done; "
  if [[ "$max_freq" =~ ^[0-9]+$ ]] && [ "$max_freq" -gt 0 ]; then
    # max_freq_mhz → KHz
    cmds+="for d in /sys/devices/system/cpu/cpufreq/policy*; do [ -w \"\$d/scaling_max_freq\" ] && echo $(( max_freq * 1000 )) > \"\$d/scaling_max_freq\" 2>/dev/null || true; done; "
    log_info "CPU 调频：governor=$governor，主频上限=${max_freq}MHz"
  fi
  if [[ "$disable_hdmi" =~ ^[Tt]rue$ ]]; then
    # tvservice 在新版 KMS 驱动上已移除，best-effort
    cmds+="command -v tvservice >/dev/null && tvservice -o 2>/dev/null || true; "
  fi
  if [[ "$wifi_ps_off" =~ ^[Tt]rue$ ]]; then
    # cherry-pick teslausb-ng 思路：禁用 brcmfmac 省电，解决 2.4G 链路抖动
    cmds+="ip link show wlan0 >/dev/null 2>&1 && iw dev wlan0 set power_save off 2>/dev/null || true; "
    log_info "WiFi 省电模式已关闭（提升视频播放流畅度）"
  fi

  cat > /etc/systemd/system/teslausb-cn-power.service <<EOF
[Unit]
Description=TeslaUSB-CN 能耗优化（CPU 调频/省电）
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/bash -c '$cmds'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable teslausb-cn-power.service >/dev/null 2>&1 || true
  # 本次安装立即生效一次
  bash -c "$cmds" 2>/dev/null || true
  log_ok "能耗优化服务已安装并启用"
}

# ================= 分区镜像（TeslaCam exFAT + LightShow FAT32） =================
create_image() { # $1=路径 $2=大小(MiB) $3=卷标 $4=强制exfat(yes/no)
  local path="$1" size_mb="$2" label="$3" force_exfat="$4"
  local loopdev fstype
  log_info "创建镜像 $path（${size_mb}MiB）..."
  truncate -s "${size_mb}M" "$path" || die "创建镜像文件失败：$path"
  loopdev="$(losetup --find --show "$path")" || die "分配 loop 设备失败"
  trap 'losetup -d "$loopdev" 2>/dev/null || true' RETURN
  if [ "$force_exfat" = "yes" ] || [ "$size_mb" -gt 32768 ]; then
    fstype="exFAT"
    mkfs.exfat -n "$label" "$loopdev" || die "exFAT 格式化失败：$label"
  else
    fstype="FAT32"
    mkfs.vfat -F 32 -n "$label" "$loopdev" || die "FAT32 格式化失败：$label"
  fi
  losetup -d "$loopdev" 2>/dev/null || true
  trap - RETURN
  log_ok "镜像已创建并格式化为 $fstype：$(basename "$path")"
}

# 在镜像内创建特斯拉要求的文件夹
ensure_folders_in_image() { # $1=镜像路径 $2=文件夹名(空格分隔)
  local path="$1" folders="$2" mnt tmpd
  tmpd="$(mktemp -d)"
  mount -o loop "$path" "$tmpd" || { log_warn "挂载镜像失败，跳过文件夹创建：$path"; rmdir "$tmpd"; return 0; }
  local f
  for f in $folders; do
    mkdir -p "$tmpd/$f"
  done
  umount "$tmpd" 2>/dev/null || true
  rmdir "$tmpd"
  log_ok "镜像 $(basename "$path") 内已创建文件夹：$folders"
}

setup_images() {
  log_info "检查分区镜像..."
  mkdir -p "$IMAGES_DIR"
  chown "$TARGET_USER:$TARGET_USER" "$IMAGES_DIR" "$REPO_DIR"

  local cam_size light_size music_size
  cam_size="$(to_mib "$(yaml_get_default partitions teslacam_size 400G)")"
  light_size="$(to_mib "$(yaml_get_default partitions lightshow_size 20G)")"
  music_size="$(to_mib "$(yaml_get_default partitions music_size 0)")"

  local existing=()
  [ -f "$IMG_CAM" ] && existing+=("usb_cam.img")
  [ -f "$IMG_LIGHTSHOW" ] && existing+=("usb_lightshow.img")
  [ -f "$IMG_MUSIC" ] && existing+=("usb_music.img")

  if [ "${#existing[@]}" -gt 0 ]; then
    log_info "检测到已存在的镜像：${existing[*]}"
    local ans
    ans="$(ask "是否保留现有镜像？(y=保留/n=删除重建)" "y")"
    if [[ "$ans" =~ ^[nN]$ ]]; then
      local confirm
      confirm="$(ask "将删除现有镜像及其中所有数据，请输入 YES 确认" "no")"
      [ "$confirm" = "YES" ] || die "已取消"
      rm -f "$IMG_CAM" "$IMG_LIGHTSHOW" "$IMG_MUSIC"
      log_warn "旧镜像已删除"
    else
      log_info "保留现有镜像，仅创建缺失的镜像"
    fi
  fi

  [ ! -f "$IMG_CAM" ] && create_image "$IMG_CAM" "$cam_size" "TeslaCam" "yes" \
    && ensure_folders_in_image "$IMG_CAM" "TeslaCam"
  [ ! -f "$IMG_LIGHTSHOW" ] && create_image "$IMG_LIGHTSHOW" "$light_size" "Lightshow" "no" \
    && ensure_folders_in_image "$IMG_LIGHTSHOW" "LightShow"
  if [ "$music_size" -gt 0 ]; then
    [ ! -f "$IMG_MUSIC" ] && create_image "$IMG_MUSIC" "$music_size" "Music" "no" \
      && ensure_folders_in_image "$IMG_MUSIC" "Music"
  fi

  # 磁盘空间提示（稀疏文件， advisory）
  local avail_mb
  avail_mb="$(df --output=avail -m "$IMAGES_DIR" | tail -1 | tr -d ' ')"
  log_info "镜像目录可用空间：${avail_mb}MiB（镜像为稀疏文件，按需占用）"
  log_ok "分区镜像就绪"
  # LightShow 分区说明：以只读方式呈现给车机可提升约 15~30% 读取性能，
  # 具体 ro 挂载由 scripts/present_usb.sh（Workstream C）在 gadget 配置时实现
}

# ================= Samba 共享 =================
setup_samba() {
  log_info "配置 Samba 共享..."
  local samba_pass="${SAMBA_PASS:-}"
  if [ -z "$samba_pass" ]; then
    local rand_pass
    rand_pass="$(tr -dc 'a-zA-Z0-9' </dev/urandom | head -c 12)"
    samba_pass="$(ask "请设置 Samba 访问密码" "$rand_pass")"
  fi

  (echo "$samba_pass"; echo "$samba_pass") | smbpasswd -s -a "$TARGET_USER" \
    || log_warn "smbpasswd 设置失败，可稍后手动执行：sudo smbpasswd -a $TARGET_USER"

  local smb_conf="/etc/samba/smb.conf"
  cp -f "$smb_conf" "${smb_conf}.bak.$(date +%s)" 2>/dev/null || true

  # 幂等：先删除旧的 teslausb-cn 共享块
  awk '
    BEGIN{skip=0}
    /^\[teslausb-cn-(cam|lightshow|music)\]/{skip=1; next}
    /^\[.*\]/{skip=0}
    {if(!skip) print}
  ' "$smb_conf" > "${smb_conf}.tmp" && mv "${smb_conf}.tmp" "$smb_conf"

  mkdir -p "$MNT_DIR/part1" "$MNT_DIR/part2" "$MNT_DIR/part3"
  cat >> "$smb_conf" <<EOF

[teslausb-cn-cam]
   path = $MNT_DIR/part1
   browseable = yes
   writable = yes
   valid users = $TARGET_USER
   guest ok = no
   create mask = 0775
   directory mask = 0775

[teslausb-cn-lightshow]
   path = $MNT_DIR/part2
   browseable = yes
   writable = yes
   valid users = $TARGET_USER
   guest ok = no
   create mask = 0775
   directory mask = 0775
EOF
  # 兼容性：SMB2+，禁用 guest
  grep -q "server min protocol" "$smb_conf" || \
    sed -i '/^\[global\]/a \   server min protocol = SMB2' "$smb_conf"
  systemctl enable smbd 2>/dev/null || true
  systemctl restart smbd 2>/dev/null || log_warn "smbd 启动失败，请手动检查"
  log_ok "Samba 共享已配置（编辑模式下挂载后可用）"
  echo ""
  log_warn "请记好 Samba 密码：$samba_pass（用户名：$TARGET_USER）"
  echo ""
}

# ================= systemd 服务（teslausb-cn- 前缀） =================
# 注意：web/app.py（Workstream B）与 scripts/*.sh（Workstream C）由其他工作线并行开发，
# 此处仅安装服务单元并 enable，文件就绪后服务即可工作。
install_systemd_services() {
  log_info "安装 systemd 服务（teslausb-cn- 前缀）..."

  # Web 界面
  cat > /etc/systemd/system/teslausb-cn-web.service <<EOF
[Unit]
Description=TeslaUSB-CN Web 管理界面
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$TARGET_USER
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/python3 $REPO_DIR/web/app.py
Restart=on-failure
RestartSec=5
Environment=TZ=Asia/Shanghai

[Install]
WantedBy=multi-user.target
EOF

  # 开机呈现 USB gadget（Edit/Present 模式切换由 Web 调用同目录脚本）
  cat > /etc/systemd/system/teslausb-cn-present.service <<EOF
[Unit]
Description=TeslaUSB-CN 开机呈现 USB 存储设备
After=teslausb-cn-web.service

[Service]
Type=oneshot
ExecStart=$REPO_DIR/scripts/boot_present.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

  # 锁车提示音定时计划
  cat > /etc/systemd/system/teslausb-cn-chime-scheduler.service <<EOF
[Unit]
Description=TeslaUSB-CN 锁车提示音定时切换

[Service]
Type=oneshot
User=$TARGET_USER
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/python3 $REPO_DIR/scripts/chime_scheduler.py --run-due
EOF
  cat > /etc/systemd/system/teslausb-cn-chime-scheduler.timer <<'EOF'
[Unit]
Description=TeslaUSB-CN 锁车提示音定时切换（每 15 分钟检查）

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
EOF

  # WiFi 监控（断线重连 / AP fallback 配合）
  cat > /etc/systemd/system/teslausb-cn-wifi-monitor.service <<EOF
[Unit]
Description=TeslaUSB-CN WiFi 监控
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/bin/bash $REPO_DIR/scripts/wifi_monitor.sh
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  for s in teslausb-cn-web.service teslausb-cn-present.service \
           teslausb-cn-chime-scheduler.timer teslausb-cn-wifi-monitor.service; do
    systemctl enable "$s" >/dev/null 2>&1 && log_info "已启用 $s" \
      || log_warn "$s 启用失败"
  done
  # Web 服务立即启动（若 app.py 尚不存在则等待 Workstream B）
  if [ -f "$REPO_DIR/web/app.py" ]; then
    systemctl restart teslausb-cn-web.service 2>/dev/null || log_warn "Web 服务启动失败"
  else
    log_warn "web/app.py 尚不存在（Workstream B 并行开发中），Web 服务将在文件就绪后可用"
  fi
  log_ok "systemd 服务安装完成"
}

# ================= rsync 封装：断线重试 + 限速 =================
# cherry-pick teslausb-ng 思路：exit 12/23/24/30 全重试；--bwlimit 防看门狗误杀。
# Workstream C 的备份代码请调用 teslausb-cn-rsync 而非裸 rsync。
install_rsync_wrapper() {
  log_info "安装 rsync 封装（teslausb-cn-rsync）..."
  cat > /usr/local/bin/teslausb-cn-rsync <<'WRAPPER_EOF'
#!/usr/bin/env bash
# TeslaUSB-CN rsync 封装：断线自动重试 + 可选限速
# 用法：teslausb-cn-rsync [rsync 参数...] 源 目标
# 环境变量 TESLAUSB_CN_RSYNC_BWLIMIT（如 5000，单位 KB/s）启用限速
set -uo pipefail
RETRIES=5
BACKOFF=10
RETRY_CODES=" 12 23 24 30 "
args=()
if [ -n "${TESLAUSB_CN_RSYNC_BWLIMIT:-}" ]; then
  args+=(--bwlimit="$TESLAUSB_CN_RSYNC_BWLIMIT")
fi
attempt=1
while true; do
  rsync "${args[@]}" "$@"
  rc=$?
  [ "$rc" -eq 0 ] && exit 0
  if [[ "$RETRY_CODES" == *" $rc "* ]] && [ "$attempt" -lt "$RETRIES" ]; then
    echo "[teslausb-cn-rsync] rsync 退出码 $rc，第 $attempt/$RETRIES 次重试（${BACKOFF}s 后）..." >&2
    sleep "$BACKOFF"
    attempt=$((attempt + 1))
    BACKOFF=$((BACKOFF * 2))
    continue
  fi
  echo "[teslausb-cn-rsync] rsync 失败，退出码 $rc" >&2
  exit "$rc"
done
WRAPPER_EOF
  chmod +x /usr/local/bin/teslausb-cn-rsync
  log_ok "rsync 封装已安装（断线重试 exit 12/23/24/30，限速经环境变量开启）"
}

# ================= 主流程 =================
main() {
  echo ""
  echo "=============================================="
  echo "  TeslaUSB-CN 安装程序"
  echo "  国行特斯拉行车记录仪管理（树莓派）"
  echo "=============================================="
  echo ""

  [ -f "$CONFIG_YAML" ] || die "未找到 $CONFIG_YAML，请在仓库根目录运行本脚本"

  detect_pi_model
  echo ""
  choose_mirror
  log_info "镜像源：$(mirror_display_name)"
  write_mirror_to_config
  echo ""
  apply_apt_mirror
  apply_pip_mirror
  echo ""
  install_packages
  echo ""
  install_rclone
  echo ""
  setup_timezone_locale
  echo ""
  setup_chrony
  echo ""
  setup_boot_config
  echo ""
  setup_power_service
  echo ""
  setup_images
  echo ""
  setup_samba
  echo ""
  install_systemd_services
  echo ""
  install_rsync_wrapper
  echo ""

  echo "=============================================="
  log_ok "TeslaUSB-CN 安装完成！"
  echo "=============================================="
  echo ""
  echo "安装摘要："
  echo "  - 设备型号：$PI_MODEL_DESC"
  echo "  - 镜像源：$(mirror_display_name)"
  echo "  - 时区：Asia/Shanghai，locale：zh_CN.UTF-8"
  echo "  - 时间同步：chrony（国内 NTP）"
  echo "  - 分区镜像：$IMAGES_DIR"
  echo "  - Web 界面：http://teslausb.local:${WEB_PORT}（或 http://<树莓派IP>:${WEB_PORT}）"
  echo "  - Samba 用户：$TARGET_USER（密码见上方提示）"
  echo ""
  log_warn "请重启树莓派使全部配置生效：sudo reboot"
  echo ""
  log_info "后续步骤："
  echo "  1. 重启后，用浏览器打开 Web 界面检查状态"
  echo "  2. 在 Web 设置页配置 WiFi（连家里 WiFi）与 NAS 备份"
  echo "  3. 将树莓派接入特斯拉 USB 口，车机应识别出 U 盘"
  echo ""
}

main "$@"
