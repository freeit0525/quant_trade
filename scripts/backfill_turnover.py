# -*- coding: utf-8 -*-
"""用证券宝(baostock)回填 daily_kline 中缺失的换手率(turnover)

背景：腾讯K线接口不提供换手率，东财被封期间数据走腾讯导致 turnover 大量为 NULL。
baostock 的 turn 字段为真实换手率，与复权无关，可全量回填。

用法: python scripts/backfill_turnover.py
幂等：对每只股票按 trade_date 匹配 UPDATE，可重复执行。
"""
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.fetcher import _baostock_ensure_login
from database.db import get_connection
from psycopg2.extras import execute_values


def fetch_turn(symbol: str, start_date: str) -> pd.DataFrame:
    """从 baostock 拉取换手率，返回 date/turnover 两列"""
    if not _baostock_ensure_login():
        return pd.DataFrame()
    import baostock as bs

    bs_code = f"sh.{symbol}" if symbol.startswith(("6", "9")) else f"sz.{symbol}"
    fmt = lambda s: f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,turn",
        start_date=fmt(start_date),
        end_date=time.strftime("%Y-%m-%d"),
        frequency="d",
        adjustflag="3",  # 原始不复权：换手率与复权无关
    )
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=rs.fields)
    df["date"] = pd.to_datetime(df["date"])
    df["turn"] = pd.to_numeric(df["turn"], errors="coerce")
    return df.rename(columns={"turn": "turnover"})


def backfill_turnover():
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("SELECT DISTINCT symbol FROM market_data.daily_kline ORDER BY symbol")
    symbols = [r[0] for r in cur.fetchall()]
    total = len(symbols)
    print(f"共 {total} 只股票需要回填换手率")

    template = "(%s::text, %s::date, %s::numeric)"
    updated = failed = 0
    for i, symbol in enumerate(symbols, 1):
        cur.execute(
            "SELECT min(trade_date) FROM market_data.daily_kline WHERE symbol = %s",
            (symbol,),
        )
        start = str(cur.fetchone()[0]).replace("-", "")
        df = fetch_turn(symbol, start)
        if df.empty:
            failed += 1
            print(f"  [{symbol}] baostock 无数据，跳过", flush=True)
            continue
        data = [
            (symbol, d.date(), (None if pd.isna(t) else float(t)))
            for d, t in zip(df["date"], df["turnover"])
        ]
        execute_values(
            cur,
            """UPDATE market_data.daily_kline AS t
               SET turnover = e.turnover
               FROM (VALUES %s) AS e(symbol, trade_date, turnover)
               WHERE t.symbol = e.symbol AND t.trade_date = e.trade_date
                 AND t.turnover IS NULL""",
            data,
            template=template,
            page_size=5000,
        )
        updated += len(data)
        if i % 200 == 0 or i == total:
            print(f"  进度: {i}/{total}（累计写入 {updated} 行，失败 {failed} 只）", flush=True)

    cur.close()
    conn.close()
    print(f"回填完成：{total} 只股票，写入 {updated} 行，失败 {failed} 只")


if __name__ == "__main__":
    backfill_turnover()
