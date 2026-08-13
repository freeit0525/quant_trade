# -*- coding: utf-8 -*-
"""补齐任务监控：实时显示已完成股票数、速率、预计剩余时间与失败股票清单

数据来源：
- 已完成数：直接查库 market_data.daily_kline 的 distinct symbol（实时且权威）
- 成功/失败计数：读取 _backfill_progress.json 与 _backfill_kline.log
- 失败清单：读取 _backfill_failed.json（权威），并展示日志中最近的重试记录
- 速率：自本监控启动以来的 (已完成增量 / 经过时间)，兼顾日志时间戳估算历史速率
用法：
- 实时监控：.venv\\Scripts\\python.exe _monitor_backfill.py [--interval 5] [--goal 5538]
- 仅查看失败清单：.venv\\Scripts\\python.exe _monitor_backfill.py --failed-only
"""
import argparse
import io
import json
import re
import sys
import time
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

PROGRESS_FILE = "_backfill_progress.json"
LOG_FILE = "_backfill_kline.log"
FAILED_FILE = "_backfill_failed.json"

LOG_OK_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] OK\s+(\S+)")
LOG_FAIL_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] FAIL\s+(\S+)")
LOG_TS_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]")
RETRY_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\s+重试\s+(\S+)")


def count_db_kline() -> int:
    from database.db import db_cursor

    with db_cursor() as cur:
        cur.execute("SELECT COUNT(DISTINCT symbol) FROM market_data.daily_kline")
        return cur.fetchone()[0]


def count_stock_info() -> int:
    from database.db import db_cursor

    with db_cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM market_data.stock_info")
        return cur.fetchone()[0]


def read_progress() -> dict:
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def read_failed() -> list[dict]:
    """读取失败清单（权威）"""
    try:
        with open(FAILED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def tail_retry_lines(n: int = 5) -> list[str]:
    """日志中最近的"重试"行（展示进行中的重试活动）"""
    retries: list[str] = []
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                if RETRY_RE.match(ln):
                    retries.append(ln.strip())
    except FileNotFoundError:
        pass
    return retries[-n:]


def tail_log_stats() -> tuple[int, int, str]:
    """统计日志中 OK/FAIL 行数，并返回最后一条 OK/FAIL 行文本"""
    ok = fail = 0
    last_result = ""
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for ln in lines:
            if LOG_OK_RE.match(ln):
                ok += 1
                last_result = ln.strip()
            elif LOG_FAIL_RE.match(ln):
                fail += 1
                last_result = ln.strip()
    except FileNotFoundError:
        pass
    return ok, fail, last_result


def log_ok_rates(window: int = 20) -> tuple[float, float]:
    """从日志 OK 行时间戳估算速率。

    window 为最近 N 条 OK 行；返回 (最近window平均每只秒数, 全程平均每只秒数)。
    时间戳仅含 HH:MM:SS，跨天时按当前日期近似处理。
    """
    import datetime as dt

    stamps: list[float] = []
    try:
        now = dt.datetime.now()
        today = now.date()
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                if not LOG_OK_RE.match(ln):
                    continue
                m = LOG_TS_RE.match(ln)
                if not m:
                    continue
                try:
                    t = dt.datetime.strptime(m.group(1), "%H:%M:%S").replace(
                        year=today.year, month=today.month, day=today.day
                    )
                except ValueError:
                    continue
                if t > now:  # 跨天：回退一天
                    t -= dt.timedelta(days=1)
                stamps.append(t.timestamp())
    except FileNotFoundError:
        return 0.0, 0.0
    if len(stamps) < 2:
        return 0.0, 0.0
    # 最近 window 条
    win = stamps[-window:]
    per_win = (win[-1] - win[0]) / (len(win) - 1) if len(win) > 1 else 0.0
    per_all = (stamps[-1] - stamps[0]) / (len(stamps) - 1) if len(stamps) > 1 else 0.0
    return max(per_win, 0.0), max(per_all, 0.0)


def fmt_eta(seconds: float) -> str:
    if seconds < 0 or seconds != seconds:  # NaN
        return "-"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    return f"{m}m {int(seconds % 60):02d}s"


def format_failed_lines(failed: list[dict], max_show: int = 10) -> list[str]:
    """格式化失败清单为输出行。max_show 限制最多展示条数"""
    if not failed:
        return ["    失败清单: 无 ✓"]
    lines = [f"    ---- 失败股票清单 ({len(failed)} 只) ----"]
    for item in failed[:max_show]:
        sym = item.get("symbol", "-")
        name = item.get("name", "")
        msg = str(item.get("msg", ""))[:60]
        t = str(item.get("time", ""))[5:19]
        lines.append(f"      {sym} {name} | {msg} | {t}")
    if len(failed) > max_show:
        lines.append(f"      ... 其余 {len(failed) - max_show} 只见 _backfill_failed.json")
    return lines


def main():
    ap = argparse.ArgumentParser(description="补齐任务监控")
    ap.add_argument("--interval", type=float, default=5.0, help="刷新间隔秒数（默认5）")
    ap.add_argument("--goal", type=int, default=0, help="目标总数，默认=stock_info 总数")
    ap.add_argument("--failed-only", action="store_true", help="只打印一次当前失败清单并退出")
    ap.add_argument("--max-failed", type=int, default=10, help="失败清单最多显示条数（默认10）")
    args = ap.parse_args()

    # 仅查看失败清单模式
    if args.failed_only:
        failed = read_failed()
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 失败股票清单（共 {len(failed)} 只）:")
        if failed:
            for item in failed:
                sym = item.get("symbol", "-")
                name = item.get("name", "")
                msg = str(item.get("msg", ""))
                t = str(item.get("time", ""))
                print(f"  {sym} {name}")
                print(f"      原因: {msg}")
                print(f"      时间: {t}")
        else:
            print("  无失败记录 ✓")
        return

    goal = args.goal or count_stock_info()
    print(f"目标股票总数: {goal}")

    # 首次采样：记录基准
    done0 = count_db_kline()
    t0 = time.time()
    first = True
    prev_done = done0
    prev_t = t0

    try:
        while True:
            time.sleep(args.interval)

            done = count_db_kline()
            now = time.time()
            elapsed = now - t0

            # 当前速率：自启动增量；日志速率：最近20只/全程（更稳）
            rate_total = (done - done0) / elapsed if elapsed > 0 else 0
            win = (done - prev_done) / (now - prev_t) if (now - prev_t) > 0 else 0
            prev_done, prev_t = done, now

            per_win, per_all = log_ok_rates()
            # 优先用日志速率（覆盖面更全），不足时用自启动速率
            sec_per = per_win if per_win > 0 else (1 / rate_total * 60 if rate_total > 0 else 0)
            sec_per_all = per_all if per_all > 0 else sec_per

            remaining = max(goal - done, 0)
            eta = remaining * sec_per
            eta_all = remaining * sec_per_all
            done_at = datetime.now().timestamp() + eta

            prog = read_progress()
            ok_cnt, fail_cnt, last_act = tail_log_stats()
            failed = read_failed()
            retry_lines = tail_retry_lines()

            pct = done / goal * 100 if goal else 0
            bar_w = 30
            filled = int(bar_w * done / goal) if goal else 0
            bar = "#" * filled + "-" * (bar_w - filled)

            lines = [
                f"[{datetime.now().strftime('%H:%M:%S')}] 已完成 {done}/{goal} ({pct:.1f}%)  [{bar}]",
                f"    速率: 近窗 {sec_per:.1f}秒/只 | 全程 {sec_per_all:.1f}秒/只 | 自启动 {(1 / rate_total * 60 if rate_total > 0 else 0):.1f}秒/只",
                f"    预计剩余: {fmt_eta(eta)} (按近窗) / {fmt_eta(eta_all)} (按全程) | 预计完成 {datetime.fromtimestamp(done_at).strftime('%H:%M')}",
                f"    进度文件: ok={prog.get('ok', '-')} failed={prog.get('failed', '-')} | 日志: OK {ok_cnt} 行 FAIL {fail_cnt} 行",
            ]
            if last_act:
                lines.append(f"    最近完成: {last_act[:100]}")
            lines.extend(format_failed_lines(failed, args.max_failed))
            if retry_lines:
                lines.append(f"    最近重试: {retry_lines[-1][:100]}")
            out = "\n".join(lines)
            # 清屏式刷新（仅 TTY 下有效）
            if sys.stdout.isatty() and not first:
                sys.stdout.write("\033[F" * len(lines))
            print(out, flush=True)
            first = False
    except KeyboardInterrupt:
        print("\n监控已停止")


if __name__ == "__main__":
    main()
