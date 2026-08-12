# -*- coding: utf-8 -*-
"""拉取大盘指数日K线入库（baostock，免费稳定）。

用法（项目虚拟环境）:
    .venv\\Scripts\\python.exe scripts\\fetch_index.py sh.000300 沪深300指数 2005-04-08

默认拉取 sh.000300（沪深300，覆盖 2005-04-08 至今），增量拉取只补缺口。
指数 symbol 以带前缀形式入库（如 sh.000300），与个股 6 位代码天然区分。
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from database.db import db_cursor, save_kline, get_kline_max_date

INDEX_DEFAULT = "sh.000300"


def _login_baostock():
    try:
        import baostock as bs
    except ImportError:
        print("未安装 baostock")
        return None
    lg = bs.login()
    if lg.error_code != "0":
        print(f"baostock 登录失败: {lg.error_msg}")
        return None
    return bs


def fetch_index(bs, code: str, name: str, list_date: str, force: bool = False):
    # 计算起始日期：库内已有时用 max_date+1，否则用上市日期；baostock 索引数据最早约 2005-01
    start = list_date if list_date else "2005-01-01"
    max_dt = get_kline_max_date(code)
    if max_dt and not force:
        start = (pd.Timestamp(max_dt) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        print(f"{code} 库内最新 {max_dt}，从 {start} 增量拉取")
    elif max_dt:
        print(f"{code} force 模式全量重拉")
    else:
        print(f"{code} 库内无数据，从 {start} 全量拉取")

    end = pd.Timestamp.today().normalize().strftime("%Y-%m-%d")
    if start > end:
        print("已是最新，无需拉取")
        return 0

    rs = bs.query_history_k_data_plus(
        code,
        "date,open,high,low,close,volume,amount",
        start_date=start, end_date=end,
        frequency="d", adjustflag="3",
    )
    if rs.error_code != "0":
        print(f"baostock 查询失败: {rs.error_code} {rs.error_msg}")
        return 0

    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        print("无新数据")
        return 0

    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["close"])
    # 插入/更新 stock_info
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO market_data.stock_info (symbol, name, list_date)
               VALUES (%s, %s, %s)
               ON CONFLICT (symbol) DO UPDATE SET name = EXCLUDED.name,
                   list_date = COALESCE(market_data.stock_info.list_date, EXCLUDED.list_date)""",
            (code, name, list_date),
        )
    n = save_kline(df, code)
    print(f"{code} 写入 {n} 行（本次拉取 {len(df)} 行，含更新）")
    return n


def main():
    code = sys.argv[1] if len(sys.argv) > 1 else INDEX_DEFAULT
    name = sys.argv[2] if len(sys.argv) > 2 else "沪深300指数"
    list_date = sys.argv[3] if len(sys.argv) > 3 else "2005-04-08"
    t0 = time.time()
    bs = _login_baostock()
    if not bs:
        sys.exit(1)
    try:
        fetch_index(bs, code, name, list_date)
    finally:
        try:
            bs.logout()
        except Exception:
            pass
    print(f"完成，耗时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
