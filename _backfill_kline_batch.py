# -*- coding: utf-8 -*-
"""分批补齐库中缺 K 线股票的日线数据（强制 baostock 源，小批慢速防三方限流）

特性：
- 断点续传：每次运行自动查询"缺 K 线的股票"（stock_info 有、daily_kline 无），已补齐的跳过；
  中途中断可随时重跑，不会重复拉取已入库的股票。
- 小批慢速：每批 batch_size 只，批间暂停 pause 秒；每只请求后 sleep 秒，避免触发接口风控。
- 失败不中断：单只失败记录到失败清单，继续下一只；失败股票下次运行自动重试。
- 进度落盘：_backfill_kline.log 记录每只结果，_backfill_progress.json 记录进度。
- 看门狗（--watchdog-sec）：baostock/远程库偶发挂起（曾单只拉取卡 80 分钟），
  超时后强制退出进程（code=86），配合 _backfill_kline_supervisor.py 自动重启续跑，
  避免整夜零进度。每次重启进程状态全新（数据库连接池/baostock 会话重置）。
"""
import argparse
import io
import json
import os
import sys
import threading
import time
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from data.fetcher import DataFetcher
from config.settings import DataSourceConfig

LOG_FILE = "_backfill_kline.log"
PROGRESS_FILE = "_backfill_progress.json"
FAILED_FILE = "_backfill_failed.json"

fetcher = DataFetcher(DataSourceConfig(source="baostock"), merge_fund_flow=False)

# ===== 看门狗：单只拉取超时强制退出（供 supervisor 识别重启）=====
WATCHDOG_EXIT = 86
_watchdog_deadline: float | None = None
_watchdog_lock = threading.Lock()


def arm_watchdog(timeout_sec: float):
    """为当前拉取设置超时截止时间"""
    global _watchdog_deadline
    with _watchdog_lock:
        _watchdog_deadline = time.time() + timeout_sec


def disarm_watchdog():
    global _watchdog_deadline
    with _watchdog_lock:
        _watchdog_deadline = None


def _watchdog_loop(timeout_sec: float):
    """后台巡检：超过截止时间仍未完成 → 强制退出进程，防止整夜卡死"""
    while True:
        with _watchdog_lock:
            dl = _watchdog_deadline
        if dl is not None and time.time() > dl:
            log(f"看门狗触发：单只拉取超过 {timeout_sec:.0f}s，进程强制退出（code={WATCHDOG_EXIT}）")
            os._exit(WATCHDOG_EXIT)
        time.sleep(5)


def log(msg: str):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_progress() -> dict:
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"total": 0, "done": 0, "ok": 0, "failed": 0, "skipped": 0, "started": str(datetime.now())}


def save_progress(p: dict):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)


def load_failed() -> list:
    try:
        with open(FAILED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_failed(lst: list):
    with open(FAILED_FILE, "w", encoding="utf-8") as f:
        json.dump(lst, f, ensure_ascii=False, indent=2)


def get_missing_symbols() -> list[tuple]:
    """查缺 K 线的股票（stock_info 有、daily_kline 无）"""
    from database.db import db_cursor

    with db_cursor() as cur:
        cur.execute("""
            SELECT si.symbol, si.name, si.list_date
            FROM market_data.stock_info si
            LEFT JOIN (SELECT DISTINCT symbol FROM market_data.daily_kline) k ON k.symbol = si.symbol
            WHERE k.symbol IS NULL
            ORDER BY si.list_date NULLS LAST, si.symbol
        """)
        return cur.fetchall()


def backfill_one(symbol: str, name: str, retries: int = 3, retry_delay: float = 5.0, watchdog_sec: float = 0.0) -> tuple[bool, str]:
    """拉取单只全历史 K 线入库。失败自动重试 retries 次（指数退避）。

    返回 (成功?, 说明)。重试基于幂等：每次调用内部都走增量逻辑，
    已入库部分不会被重复写入。
    """
    last_err = ""
    for attempt in range(retries + 1):
        if attempt > 0:
            wait = retry_delay * (2 ** (attempt - 1))
            log(f"    重试 {symbol} {name} 第 {attempt}/{retries} 次（等待 {wait:.0f}s）")
            time.sleep(wait)
        try:
            arm_watchdog(watchdog_sec)
            try:
                df = fetcher.fetch_stock(symbol, "19900101", "20991231", use_db=True)
            finally:
                disarm_watchdog()
            n = 0 if df is None else len(df)
            if n > 0:
                return True, f"入库 {n} 条"
            last_err = "返回空数据"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    return False, f"重试{retries}次仍失败: {last_err}"


def main():
    ap = argparse.ArgumentParser(description="分批补齐缺 K 线股票")
    ap.add_argument("--batch-size", type=int, default=100, help="每批股票数（默认100）")
    ap.add_argument("--pause", type=int, default=30, help="批间暂停秒数（默认30）")
    ap.add_argument("--sleep", type=float, default=0.8, help="每只请求后 sleep 秒（默认0.8）")
    ap.add_argument("--max-batches", type=int, default=0, help="本次最多处理批数，0=全部（默认0）")
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理股票数（调试用，0=不限）")
    ap.add_argument("--retries", type=int, default=3, help="单只失败自动重试次数（默认3，指数退避）")
    ap.add_argument("--retry-delay", type=float, default=5.0, help="首次重试等待秒数（默认5，之后翻倍）")
    ap.add_argument("--watchdog-sec", type=float, default=480.0,
                    help="看门狗：单只拉取超过该秒数强制退出进程（code=86，默认480=8分钟；0=关闭）")
    args = ap.parse_args()

    if args.watchdog_sec > 0:
        threading.Thread(target=_watchdog_loop, args=(args.watchdog_sec,), daemon=True).start()
        log(f"看门狗已启动（单只拉取超过 {args.watchdog_sec:.0f}s 强制退出，code={WATCHDOG_EXIT}）")

    log("=" * 60)
    log(f"开始分批补齐（batch={args.batch_size}, pause={args.pause}s, sleep={args.sleep}s, max_batches={args.max_batches}, retries={args.retries}）")

    missing = get_missing_symbols()
    total = len(missing)
    log(f"当前缺 K 线股票数: {total}")
    if total == 0:
        log("无需补齐，退出")
        return

    progress = load_progress()
    progress["total"] = total
    failed_prev = load_failed()

    batches = [missing[i:i + args.batch_size] for i in range(0, total, args.batch_size)]
    if args.max_batches > 0:
        batches = batches[:args.max_batches]
    log(f"共 {len(batches)} 批")

    n_done = 0
    for bi, batch in enumerate(batches, 1):
        log(f"---- 第 {bi}/{len(batches)} 批（本批 {len(batch)} 只） ----")
        for sym, name, list_date in batch:
            if args.limit and n_done >= args.limit:
                log(f"达到 --limit {args.limit}，停止")
                save_progress(progress)
                save_failed(failed_prev)
                return
            ok, msg = backfill_one(sym, name, retries=args.retries, retry_delay=args.retry_delay, watchdog_sec=args.watchdog_sec)
            n_done += 1
            progress["done"] = n_done
            if ok:
                progress["ok"] += 1
                log(f"OK   {sym} {name}: {msg}")
            else:
                progress["failed"] += 1
                failed_prev.append({"symbol": sym, "name": name, "msg": msg, "time": str(datetime.now())})
                log(f"FAIL {sym} {name}: {msg}")
            time.sleep(args.sleep)
        save_progress(progress)
        save_failed(failed_prev)
        log(f"---- 本批完成，累计 {n_done}/{len(batches) * args.batch_size if args.max_batches == 0 else n_done} ----")
        if bi < len(batches):
            log(f"批间暂停 {args.pause} 秒...")
            time.sleep(args.pause)

    save_progress(progress)
    save_failed(failed_prev)
    log("=" * 60)
    log(f"本次结束: 成功 {progress['ok']}, 失败 {progress['failed']}（失败清单见 {FAILED_FILE}，重跑自动重试）")


if __name__ == "__main__":
    main()
