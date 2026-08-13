# -*- coding: utf-8 -*-
"""用东财直连批量补齐全库股票近120个交易日资金流（覆盖已有值，幂等）

背景：K线补齐任务每只入库时若东财失败会落到新浪兜底（口径不可靠），
库中现有资金流可能混有新浪数据。本脚本用东财数据覆盖近120日，
保证库中近期资金流为东财口径。东财接口被风控时会快速失败跳过，
可等风控缓解后重跑（断点续传：已成功股票记入进度文件，跳过）。
"""
import argparse
import io
import json
import sys
import time
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from data.fetcher import DataFetcher
from database.db import db_cursor

LOG_FILE = "_backfill_ff.log"
PROGRESS_FILE = "_backfill_ff_progress.json"
FAILED_FILE = "_backfill_ff_failed.json"
FLOW_COLS = ["main_net_inflow", "super_large_net_inflow",
             "large_net_inflow", "medium_net_inflow", "small_net_inflow"]

fetcher = DataFetcher()  # 默认源，资金流直接调东财专用函数


def log(msg: str):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: str, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def get_all_symbols() -> list[str]:
    """库中所有有 K 线的股票"""
    with db_cursor() as cur:
        cur.execute("SELECT DISTINCT symbol FROM market_data.daily_kline ORDER BY symbol")
        return [r[0] for r in cur.fetchall()]


def overwrite_fund_flow(symbol: str, df) -> int:
    """把东财资金流 DataFrame 按日期覆盖写入库，返回更新行数"""
    if df is None or df.empty:
        return 0
    updated = 0
    with db_cursor() as cur:
        for _, row in df.iterrows():
            cur.execute(
                f"""UPDATE market_data.daily_kline
                    SET {FLOW_COLS[0]}=%s, {FLOW_COLS[1]}=%s, {FLOW_COLS[2]}=%s,
                        {FLOW_COLS[3]}=%s, {FLOW_COLS[4]}=%s
                    WHERE symbol=%s AND trade_date=%s""",
                (float(row["main_net_inflow"]), float(row["super_large_net_inflow"]),
                 float(row["large_net_inflow"]), float(row["medium_net_inflow"]),
                 float(row["small_net_inflow"]), symbol, row["date"].date()),
            )
            updated += cur.rowcount
    return updated


def backfill_one(symbol: str, retries: int = 2, retry_delay: float = 5.0) -> tuple[bool, str]:
    last_err = ""
    for attempt in range(retries + 1):
        if attempt > 0:
            wait = retry_delay * (2 ** (attempt - 1))
            log(f"    重试 {symbol} 第 {attempt}/{retries} 次（等待 {wait:.0f}s）")
            time.sleep(wait)
        try:
            ff = fetcher._fetch_fund_flow_via_eastmoney(symbol)
            if ff is None or ff.empty:
                last_err = "东财返回空（可能被风控或无数据）"
                continue
            n = overwrite_fund_flow(symbol, ff)
            if n > 0:
                return True, f"覆盖 {n} 行 ({ff['date'].min().date()} ~ {ff['date'].max().date()})"
            last_err = "覆盖 0 行（K线库中无对应日期）"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    return False, f"重试{retries}次仍失败: {last_err}"


def probe_eastmoney() -> bool:
    """探测东财资金流接口是否可用（用 000636 实测）"""
    try:
        ff = fetcher._fetch_fund_flow_via_eastmoney("000636")
        return ff is not None and not ff.empty
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="东财资金流批量补齐（全量覆盖近120日）")
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理股票数（0=全部）")
    ap.add_argument("--sleep", type=float, default=1.0, help="每只请求后 sleep 秒（默认1.0，防风控）")
    ap.add_argument("--retries", type=int, default=2, help="单只失败重试次数（默认2）")
    ap.add_argument("--retry-delay", type=float, default=5.0, help="首次重试等待秒数（默认5，翻倍）")
    ap.add_argument("--force", action="store_true", help="忽略进度，全量重新覆盖")
    ap.add_argument("--wait-recovery", type=int, default=0,
                    help="东财被风控时自动等待恢复（分钟；0=不等待直接开始，默认0）")
    args = ap.parse_args()

    log("=" * 60)
    log(f"开始东财资金流补齐（sleep={args.sleep}s, retries={args.retries}, force={args.force}）")

    if args.wait_recovery > 0:
        waited = 0
        while not probe_eastmoney():
            waited += 1
            log(f"东财接口不可用（风控/网络），已等待 {waited} 分钟，继续探测...")
            time.sleep(60)
            if waited >= args.wait_recovery:
                log(f"等待超过 {args.wait_recovery} 分钟仍未恢复，放弃本次运行")
                return
        log("东财接口已恢复，开始补齐")

    symbols = get_all_symbols()
    log(f"库中共 {len(symbols)} 只有 K 线的股票")

    progress = load_json(PROGRESS_FILE, {"ok": 0, "failed": 0, "done": 0, "done_symbols": []})
    failed_prev = load_json(FAILED_FILE, [])
    done_set = set(progress.get("done_symbols", []))

    todo = [s for s in symbols if args.force or s not in done_set]
    total = len(todo)
    log(f"需处理 {total} 只（已成功 {len(done_set)} 只跳过）")
    if not total:
        log("全部已完成，退出")
        return

    for i, sym in enumerate(todo, 1):
        if args.limit and progress["done"] - (len(symbols) - len(todo)) >= args.limit:
            log(f"达到 --limit {args.limit}，停止")
            break
        ok, msg = backfill_one(sym, retries=args.retries, retry_delay=args.retry_delay)
        progress["done"] += 1
        if ok:
            progress["ok"] += 1
            progress["done_symbols"].append(sym)
            log(f"OK   [{i}/{total}] {sym}: {msg}")
        else:
            progress["failed"] += 1
            failed_prev.append({"symbol": sym, "msg": msg, "time": str(datetime.now())})
            log(f"FAIL [{i}/{total}] {sym}: {msg}")
        time.sleep(args.sleep)
        if i % 20 == 0:
            save_json(PROGRESS_FILE, progress)
            save_json(FAILED_FILE, failed_prev)

    save_json(PROGRESS_FILE, progress)
    save_json(FAILED_FILE, failed_prev)
    log("=" * 60)
    log(f"本次结束: 成功 {progress['ok']}, 失败 {progress['failed']}（失败清单见 {FAILED_FILE}，重跑自动重试）")


if __name__ == "__main__":
    main()
