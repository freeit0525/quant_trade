# -*- coding: utf-8 -*-
"""补全 stock_info 表缺失的上市日期（及北交所行业/股本）

现状：北交所 333 只上市日期全空、沪市 612 只空（深市已全）。
数据源：
  1. 沪深上市日期: baostock query_stock_basic 的 ipoDate
  2. 北交所上市日期 + 行业 + 总股本/流通股本: akshare stock_info_bj_name_code（北交所官网）

用法: python scripts/backfill_list_date.py
幂等：只更新缺失字段（COALESCE 保护已有值），可重复执行。
"""
import sys
from pathlib import Path

import pandas as pd
from psycopg2.extras import execute_values

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import akshare as ak
from data.fetcher import _baostock_ensure_login
from database.db import get_connection


def fill_sh_sz_list_date(cur):
    """baostock 补沪深上市日期"""
    print("获取 baostock 股票基本信息（ipoDate）...")
    if not _baostock_ensure_login():
        print("baostock 登录失败，跳过沪深")
        return
    import baostock as bs

    rs = bs.query_stock_basic()
    rows = []
    while rs.next():
        r = rs.get_row_data()
        if r[4] != "1":  # type: 1=股票，2=指数
            continue
        code = r[0].split(".")[1]  # sh.600000 -> 600000
        ipo = r[2].strip()
        if ipo:
            rows.append((code, ipo))
    print(f"baostock 股票 {len(rows)} 只")

    sql = """
        UPDATE market_data.stock_info SET list_date = CAST(v.list_date AS date)
        FROM (VALUES %s) AS v(symbol, list_date)
        WHERE market_data.stock_info.symbol = v.symbol
          AND market_data.stock_info.list_date IS NULL
    """
    execute_values(cur, sql, rows, page_size=500)
    print(f"沪深上市日期更新：{len(rows)} 条（仅缺失的会生效）")


def fill_bj_info(cur):
    """akshare 北交所接口补上市日期/行业/股本（北交所官网，不受东财风控影响）"""
    print("获取北交所股票列表...")
    df = ak.stock_info_bj_name_code()
    print(f"北交所 {len(df)} 只")

    def to_yi_shares(val):
        """股 -> 亿股"""
        if val is None or pd.isna(val):
            return None
        try:
            return round(float(str(val).replace(",", "")) / 1e8, 4)
        except (ValueError, TypeError):
            return None

    rows = []
    for _, r in df.iterrows():
        code = str(r["证券代码"]).zfill(6)
        list_date = str(r["上市日期"]).strip() if pd.notna(r["上市日期"]) else None
        industry = r.get("所属行业")
        if isinstance(industry, float) and pd.isna(industry):
            industry = None
        rows.append((
            code,
            list_date if list_date and list_date != "nan" else None,
            to_yi_shares(r.get("总股本")),
            to_yi_shares(r.get("流通股本")),
            industry,
        ))

    sql = """
        UPDATE market_data.stock_info AS t SET
            list_date = COALESCE(t.list_date, CAST(v.list_date AS date)),
            total_share = COALESCE(t.total_share, CAST(v.total_share AS numeric)),
            float_share = COALESCE(t.float_share, CAST(v.float_share AS numeric)),
            industry = COALESCE(NULLIF(t.industry, ''), v.industry),
            updated_at = CURRENT_TIMESTAMP
        FROM (VALUES %s) AS v(symbol, list_date, total_share, float_share, industry)
        WHERE t.symbol = v.symbol
    """
    execute_values(cur, sql, rows, page_size=500)
    print(f"北交所信息更新：{len(rows)} 条")


def main():
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()
    fill_sh_sz_list_date(cur)
    fill_bj_info(cur)
    cur.close()
    conn.close()
    print("完成")


if __name__ == "__main__":
    main()
