# -*- coding: utf-8 -*-
"""资金流补齐任务监督进程：工作进程被看门狗（单只拉取超时）强制退出后自动重启续跑

用法（透传 _backfill_fund_flow.py 的全部参数）：
  python _backfill_fund_flow_supervisor.py --sleep 1.0 --retries 2 --watchdog-sec 480

- 工作进程正常结束（全部补齐或达到 --limit）→ 监督进程退出
- 工作进程被看门狗杀死（退出码 86）→ 等待后自动重启（断点续传，进程状态全新）
- 连续重启次数越多，等待越久（10s 起步，每多一次 +10s，上限 60s）
"""
import io
import os
import subprocess
import sys
import time
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

LOG_FILE = "_backfill_ff.log"
WATCHDOG_EXIT = 86
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def log(msg: str):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] [supervisor] {msg}"
    print(line, flush=True)
    with open(os.path.join(BASE_DIR, LOG_FILE), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    args = sys.argv[1:]
    restart_count = 0
    while True:
        cmd = [sys.executable, "_backfill_fund_flow.py"] + args
        log(f"启动工作进程（连续重启 {restart_count} 次）: {' '.join(cmd)}")
        proc = subprocess.run(cmd, cwd=BASE_DIR)
        code = proc.returncode
        if code != WATCHDOG_EXIT:
            log(f"工作进程结束（code={code}），监督进程退出")
            return
        restart_count += 1
        wait = min(10 + restart_count * 10, 60)
        log(f"工作进程被看门狗强制退出（code={WATCHDOG_EXIT}），{wait}s 后重启续跑...")
        time.sleep(wait)


if __name__ == "__main__":
    main()
