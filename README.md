# TeslaUSB-CN

为国行特斯拉深度适配的行车记录仪管理方案。

把树莓派变成车载双分区 U 盘（行车记录 + 灯光秀/锁车提示音），通过全中文 Web 界面管理视频，并自动备份到家中 NAS。

## 特性

- **双分区 USB**：TeslaCam（行车记录/哨兵）+ LightShow（灯光秀/锁车提示音），可选 Music 分区
- **全中文 Web 界面**：视频浏览/播放、锁车提示音、灯光秀、备份、设置，移动端优先
- **NAS 自动备份**：rclone 通用 remote（SMB/NFS/SFTP/WebDAV），备份后可选自动删除本地
- **国内开箱即用**：一键切换国内镜像源（apt/pip/GitHub/gitee）、默认时区 Asia/Shanghai
- **低功耗优化**：为树莓派 Zero 2W 手套箱场景调优（USB 口直供、散热与能耗策略）

---

## 部署教程：把树莓派变成车载 U 盘服务器

整个流程约 30–60 分钟，大部分时间在等系统安装。

### 一、准备硬件

| 硬件 | 说明 |
|---|---|
| 树莓派 | **Zero 2W（推荐，放手套箱）**；备选 Pi 4；Pi 5 功耗太大不推荐车机直供 |
| microSD 卡 | 128GB 以上**高耐久卡**（如三星 PRO Endurance / 闪迪 High Endurance），行车记录 7×24 写入，普通卡扛不住 |
| 读卡器 | 烧录系统用 |
| 数据线 | **必须带数据传输功能**，不能用纯充电线。Zero 2W 接中间的 USB 口（不是 PWR 口）；Pi 4 接 USB-C 口 |
| 电脑 | 烧录系统、SSH 登录用 |

> **车机版本要求**：车机需早于 2026.20。如果车机设置里出现了"行车记录仪加密"选项，**请先关闭它**——加密开启后车外任何设备都读不到视频，本项目也无能为力。

### 二、烧录系统

1. 在电脑上安装 [Raspberry Pi Imager](https://www.raspberrypi.com/software/)。
2. 选择系统：**Raspberry Pi OS Lite（64-bit）**，不要桌面版（省内存、省电）。
3. 点右下角齿轮（或"编辑设置"），预配置：
   - 主机名：`teslausb`
   - 用户名/密码：设一个你记得住的（如 `pi` / 自定义密码）
   - **开启 SSH**（用密码登录）
   - **配置 WiFi**：填家里 WiFi 的名称和密码（国家选 CN）
4. 选择 SD 卡，烧录，完成后把卡插到树莓派上，上电开机。
5. 等 1–2 分钟，在电脑上 SSH 登录：
   ```bash
   ssh pi@teslausb.local
   # 如果 .local 解析不了，用路由器后台查树莓派的 IP：ssh pi@<IP>
   ```

### 三、一键安装

登录后执行（全程中文提示，跟着走就行）：

```bash
# 1. 装 git（Lite 版默认没有）
sudo apt update && sudo apt install -y git

# 2. 拉代码
git clone https://github.com/Richard-M-L/TeslaUSBServer.git
cd TeslaUSBServer

# 3. 一键安装（国内用户务必加 --china-mirror）
sudo bash setup.sh --china-mirror
```

安装脚本会依次做这些事（约 10–20 分钟，可重复运行，已完成的步骤会自动跳过）：

1. **换国内镜像源**：交互三选一（清华 / 中科大 / 阿里云），apt、pip、rclone、GitHub 全部走国内
2. **检测树莓派型号**：Zero 2W / 4 / 5 自动识别；Pi 4/5 自动写入 `otg_mode=1`（USB-C 做 U 盘设备的必需开关），并按型号提示供电/散热注意事项
3. **设时区和中文环境**：Asia/Shanghai + `zh_CN.UTF-8`
4. **装依赖**：分区工具、Samba、ffmpeg、rsync、chrony（国内 NTP 对时）等，只装缺的
5. **能耗优化**：关 HDMI/LED/蓝牙、CPU 降频上限、**关闭 WiFi 省电模式**（2.4G 链路更稳，看视频不卡）
6. **创建双分区 U 盘镜像**：`images/` 下生成两个镜像文件——TeslaCam（exFAT，行车记录）和 LightShow（FAT32，灯光秀/提示音），大小可在 `config.yaml` 里改
7. **装系统服务**：Web 界面、开机自动呈现 U 盘、提示音定时器、WiFi 监控，全部开机自启

装完按提示重启：

```bash
sudo reboot
```

### 四、Web 界面初始配置

重启后，在**连着同一个 WiFi** 的手机/电脑浏览器打开：

```
http://teslausb.local:5000
```

首页先确认三件事：设备状态显示"USB 模式"、存储分区两条进度条正常、系统状态无报错。然后去**设置**页：

1. **无线网络**：确认连上的是家里 WiFi（信号强度越高越好，备份和看视频都走这条链路）
2. **备份到 NAS**（可选）：填 NAS 的协议/地址/端口/账号密码，点"保存配置"；"备份成功后自动删除本地"默认关闭，存储实在紧张再开（有二次确认）
3. **镜像源**：如果以后想换源，不用重跑脚本，在这里一键切换

### 五、上车：接入特斯拉

1. 树莓派**断电状态下**，用数据线把 Pi 的数据口接到车内 USB 口（推荐**手套箱**里的口，隐蔽且供电稳）。
2. Pi 上电后会自动把自己呈现成 U 盘（`teslausb-cn-present` 服务，开机即 present，无需任何操作）。
3. 车机状态栏出现**行车记录仪图标**即成功。点图标可手动保存；哨兵模式触发的会自动存到 SentryClips。
4. **锁车提示音**：在 Web 的"提示音"页上传 WAV（1MB 以内），设为当前，锁车即生效（国行车机实测可用）。
5. **灯光秀**：在"灯光秀"页上传 `.fseq` + `.mp3` 配对文件，车机灯光秀菜单里就能选到。

### 六、日常使用

**两种模式**（首页一键切换，切换有二次确认）：

- **USB 模式（默认）**：Pi 伪装成 U 盘给车用。开车、停车哨兵都用这个模式，**Web 端只能看不能改文件**。
- **编辑模式**：Pi 断开模拟，镜像以可读写方式挂载到 Pi 本地。这时才能在 Web 里删除视频、整理文件。**编辑完记得切回 USB 模式再上车**，否则车机认不到盘。

**典型流程**：

- **看视频**：连家里 WiFi → 打开 Web → 视频页按 RecentClips / SavedClips / SentryClips 筛选 → 点进去六路同播（前/左前/右前/左B柱/右B柱/后视）
- **备份**：设置页配好 NAS → 点"立即备份"，或等它自动备；备份进度实时显示
- **清空间**：设置页"自动清理视频"，按文件夹设保留天数（如最近录像保留 7 天），可设开机自动清理
- **换提示音**：提示音页上传/定时计划/随机分组，开机随机每天给你换一个锁车声

### 七、故障排查

| 现象 | 查什么 |
|---|---|
| 车机不识别 U 盘 | ① 数据线是不是纯充电线 ② Zero 2W 是否插的中间 USB 口（非 PWR）③ Pi 4/5 确认 `config.txt` 里有 `otg_mode=1`（安装脚本已自动写）④ SSH 上去看 `systemctl status teslausb-cn-present` |
| Web 打不开 | ① Pi 和手机是否同一 WiFi ② 试 `http://<树莓派IP>:5000` ③ `systemctl status teslausb-cn-web` |
| 视频时间显示不对 | 设置页确认时区是 Asia/Shanghai；拿一个真实录像对比文件名时间（文件名时间即车机本地时间） |
| WiFi 连不上 / 备份失败 | 设置页"无线网络"里重连；或打开 AP 热点模式，手机直连 Pi 排查 |
| Pi 频繁重启 / 欠压 | Pi 4 在车机 USB 口供电可能不稳，SSH 跑 `vcgencmd get_throttled`，非 0 即欠压，换供电更好的口或加 powered hub |
| 灯光秀车机里没有 | 确认 `.fseq` 和 `.mp3` 文件名一致且都上传成功；切到编辑模式检查镜像里 `LightShow/` 目录 |

### 八、常用命令速查

```bash
# 看服务状态
systemctl status teslausb-cn-web teslausb-cn-present

# 看实时日志
journalctl -u teslausb-cn-web -f

# 手动切换 USB/编辑模式（等同于 Web 首页按钮）
sudo bash scripts/present_usb.sh
sudo bash scripts/edit_usb.sh

# 重跑安装脚本（幂等，修东西/换源都用它）
sudo bash setup.sh --china-mirror

# 备份的配置文件
cat config.yaml
```

---

## 项目状态

Phase 1（骨架）已完成：精简安装脚本、全中文 Web 界面、内核移植（SEI 解析/归档/NAS/提示音）、P0 修复。规划见 `docs/PROJECT_PLAN.md`。

## 致谢

内核移植自 [RdeLange/TeslaUSB](https://github.com/RdeLange/TeslaUSB) 及
[Richard-M-L/TeslaUSB20260521](https://github.com/Richard-M-L/TeslaUSB20260521)；
部分补丁思路来自 [danusha2345/teslausb-ng](https://github.com/danusha2345/teslausb-ng)。

## 开源协议

MIT（见 LICENSE）。
