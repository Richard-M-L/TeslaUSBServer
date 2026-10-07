# dashcam.proto 编译说明

本目录的 `dashcam.proto` 描述特斯拉行车记录仪 MP4 中 SEI NAL 单元里
protobuf 编码的遥测数据（`SeiMetadata`：车速、挡位、方向盘、踏板、
转向灯、Autopilot 状态、GPS 等），供 `web/services/sei_parser.py` 使用。

## 手动编译（推荐在 Pi 上做一次）

```bash
# 1. 安装 protoc
sudo apt install -y protobuf-compiler

# 2. 在仓库根执行，产物生成到 web/services/dashcam_pb2.py
cd <仓库根>
protoc --python_out=web/services \
       --proto_path=web/static \
       web/static/dashcam.proto
```

同时需要 Python 的 protobuf 运行时（二选一）：

```bash
pip install protobuf        # 标准运行时
# 或
pip install protobuf-lite   # 精简版（Pi Zero 2 W 推荐，体积更小）
```

> 注意：protoc 主版本与 Python protobuf 运行时主版本建议一致
> （如 protoc 4.x 配 protobuf 4.x），否则生成的 `dashcam_pb2.py`
> 可能报版本不兼容。Pi 上如遇此问题，删掉 `dashcam_pb2.py` 让
> `sei_parser` 走下面的自动编译，或统一用 apt 的版本
> （`python3-protobuf` + `protobuf-compiler` 来自同一发行版，最稳）。

## 启动时自动编译

`sei_parser._get_sei_metadata_class()` 的加载顺序：

1. 尝试 `from .dashcam_pb2 import SeiMetadata`（已编译好的模块）；
2. 若缺失，检查 `web/static/dashcam.proto` 是否存在，存在则调用
   系统 `protoc` 自动编译到 `web/services/dashcam_pb2.py`，
   成功后导入使用（记一条 warning 日志）。

自动编译只在**首次**解析 SEI 时触发一次，之后走缓存。

## 纯 Python 降级（无 protobuf 时）

Pi 上如果**既没装 protobuf 运行时、又没有 protoc**（或 proto 源缺失）：

- `_get_sei_metadata_class()` 返回 `None` 并记一条**中文 warning**，
  **不抛异常**；
- `extract_sei_messages()` 直接返回空结果（生成器不产生任何消息），
  并记中文 warning；
- `parse_video_sei()` 返回空列表 `[]`。

即：缺少 protobuf 只会让 SEI 遥测解析静默跳过，不会导致 Web 或
后台任务崩溃。需要恢复遥测功能时，按上面的手动编译步骤安装即可，
无需改代码。
