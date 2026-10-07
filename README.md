# TeslaUSB-CN

为国行特斯拉深度适配的行车记录仪管理方案。

把树莓派变成车载双分区 U 盘（行车记录 + 灯光秀/锁车提示音），通过全中文 Web 界面管理视频，并自动备份到家中 NAS。

## 特性

- **双分区 USB**：TeslaCam（行车记录/哨兵）+ LightShow（灯光秀/锁车提示音），可选 Music 分区
- **全中文 Web 界面**：视频浏览/播放、锁车提示音、灯光秀、备份、设置，移动端优先
- **NAS 自动备份**：rclone 通用 remote（SMB/NFS/SFTP/WebDAV），备份后可选自动删除本地
- **国内开箱即用**：一键切换国内镜像源（apt/pip/GitHub/gitee）、默认时区 Asia/Shanghai
- **低功耗优化**：为树莓派 Zero 2W 手套箱场景调优（USB 口直供、散热与能耗策略）

## 硬件要求

- 树莓派 Zero 2W（主力，车机 USB 口直供）/ 4 / 5（备选）
- 128GB 以上高耐久 microSD 卡（三星 PRO Endurance / 闪迪 High Endurance）
- 特斯拉（国行，2026.20 以下版本；若车机开启行车记录加密请先关闭）

## 快速开始

```bash
# 在树莓派上执行（国内用户建议加 --china-mirror）
sudo bash setup.sh --china-mirror
```

详细文档见 `docs/`。

## 项目状态

Phase 1 开发中（骨架阶段）。规划书见 `docs/PROJECT_PLAN.md`（或仓库附件）。

## 致谢

内核移植自 [RdeLange/TeslaUSB](https://github.com/RdeLange/TeslaUSB) 及
[Richard-M-L/TeslaUSB20260521](https://github.com/Richard-M-L/TeslaUSB20260521)；
部分补丁思路来自 [danusha2345/teslausb-ng](https://github.com/danusha2345/teslausb-ng)。

## 开源协议

MIT（见 LICENSE）。
