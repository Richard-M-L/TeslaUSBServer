#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TeslaUSB-CN 开机清理钩子。

由 scripts/boot_present.sh 在呈现 USB 前调用（可选）：
仅当 config.yaml 中 cleanup.run_on_boot=true 时启动后台清理任务；
任何异常都不应阻塞开机流程（调用方已做保护，本脚本自身也捕获全部异常）。
"""
import os
import sys
import time

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)


def main() -> int:
    try:
        from web.services import cleanup_service
        from web.services.jobs import get_job_progress
    except Exception as e:  # noqa: BLE001
        print("开机清理：服务模块加载失败，跳过：%s" % e)
        return 0

    try:
        job_id = cleanup_service.boot_cleanup_if_enabled()
    except Exception as e:  # noqa: BLE001
        print("开机清理：启动失败，跳过：%s" % e)
        return 0

    if not job_id:
        print("开机清理：未启用（cleanup.run_on_boot=false），跳过。")
        return 0

    # 等待清理任务结束（最多 5 分钟），不阻塞则直接返回也可；
    # 这里等待是为了让日志完整，超时不算失败。
    deadline = time.time() + 300
    while time.time() < deadline:
        try:
            s = get_job_progress(job_id)
        except Exception:  # noqa: BLE001
            break
        if s.get("state") in ("done", "failed", "cancelled"):
            print("开机清理：结束，状态=%s，%s" % (s.get("state"), s.get("message") or ""))
            break
        time.sleep(2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
