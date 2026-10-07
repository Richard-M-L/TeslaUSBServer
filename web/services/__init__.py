"""TeslaUSB-CN Web 服务层（纯 Python，不依赖 Flask）。

本包提供 Phase 1 基础能力，供 Web 蓝图与后台任务共用：

- ``config``：仓库根 ``config.yaml`` 集中配置读取 + 环境变量覆盖
- ``tzutil``：车机时区工具（时区 P0 规范：绝不裸用 ``datetime.fromtimestamp()``）
- ``jobs``：后台长耗时任务管理（线程实现，中文进度回调）
- ``sei_parser``：特斯拉行车记录 MP4 的 SEI 遥测解析器（mmap 低内存版，
  移植自上游 fork，纯解析能力）

设计约定：服务层只依赖标准库 + ``yaml``，不导入 Flask，便于单测与脚本复用。
用户可见的日志 / 进度消息一律中文。
"""
