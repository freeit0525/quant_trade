"""补全 stock_info 表的缺失字段

数据源（全部基于 akshare，非东方财富）：
  1. 沪市上市日期: stock_info_sh_name_code()
  2. 深市上市日期 + 总股本 + 流通股本 + 行业: stock_info_sz_name_code('A股列表')
  3. 沪市总股本/流通股本: 从新浪 stock_classify_sina() 的市值÷股价反算

用法：
  python scripts/fill_stock_info.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import akshare as ak
import pandas as pd
from psycopg2.extras import execute_values

from database.db import get_connection


def fill_sh_list_date(cur):
    """沪市上市日期"""
    print("获取沪市股票列表（含上市日期）...")
    df = ak.stock_info_sh_name_code()
    print(f"沪市 {len(df)} 只")

    rows = []
    for _, r in df.iterrows():
        code = str(r["证券代码"]).zfill(6)
        list_date = str(r["上市日期"]).strip() if pd.notna(r["上市日期"]) else None
        if list_date and list_date != "nan":
            rows.append((list_date, code))

    sql = """
        UPDATE market_data.stock_info SET list_date = CAST(v.list_date AS date)
        FROM (VALUES %s) AS v(list_date, symbol)
        WHERE market_data.stock_info.symbol = v.symbol
          AND market_data.stock_info.list_date IS NULL
    """
    execute_values(cur, sql, rows, page_size=500)
    print(f"沪市上市日期更新：{len(rows)} 条")


def fill_sz_info(cur):
    """深市上市日期 + 总股本 + 流通股本 + 行业"""
    print("获取深市股票列表（含上市日期/股本/行业）...")
    df = ak.stock_info_sz_name_code(symbol="A股列表")
    print(f"深市 {len(df)} 只")

    rows = []
    for _, r in df.iterrows():
        code = str(r["A股代码"]).zfill(6)
        list_date = str(r["A股上市日期"]).strip() if pd.notna(r["A股上市日期"]) else None
        if list_date and list_date == "nan":
            list_date = None

        # 总股本/流通股本：原始值为字符串含逗号，单位为"股"，转为"亿股"
        def to_yi_shares(val):
            if val is None or pd.isna(val):
                return None
            try:
                return round(float(str(val).replace(",", "")) / 1e8, 4)
            except (ValueError, TypeError):
                return None

        total_share = to_yi_shares(r.get("A股总股本"))
        float_share = to_yi_shares(r.get("A股流通股本"))
        industry = r.get("所属行业")
        if isinstance(industry, float) and pd.isna(industry):
            industry = None

        rows.append((list_date, total_share, float_share, industry, code))

    sql = """
        UPDATE market_data.stock_info AS t SET
            list_date = COALESCE(t.list_date, CAST(v.list_date AS date)),
            total_share = COALESCE(t.total_share, CAST(v.total_share AS numeric)),
            float_share = COALESCE(t.float_share, CAST(v.float_share AS numeric)),
            industry = COALESCE(NULLIF(t.industry, ''), v.industry),
            updated_at = CURRENT_TIMESTAMP
        FROM (VALUES %s) AS v(list_date, total_share, float_share, industry, symbol)
        WHERE t.symbol = v.symbol
    """
    execute_values(cur, sql, rows, page_size=500)
    print(f"深市信息更新：{len(rows)} 条")


def fill_sh_shares(old_cur):
    """沪市总股本/流通股本：从新浪市值÷股价反算"""
    print("获取新浪行情数据（用于反算沪市股本）...")
    df = ak.stock_classify_sina()
    df_a = df[df["symbol"].str.match(r"^sh\d{6}$")].copy()
    df_a["code"] = df_a["code"].astype(str).str.zfill(6)
    df_a = df_a.drop_duplicates(subset=["code"], keep="last")
    print(f"沪市 {len(df_a)} 条")

    rows = []
    for _, r in df_a.iterrows():
        code = str(r["code"]).zfill(6)
        price = r.get("trade")
        mktcap = r.get("mktcap")  # 万元
        nmc = r.get("nmc")        # 万元

        if price is None or pd.isna(price) or float(price) == 0:
            continue

        # 总股本(亿股) = 总市值(万元) / 股价 / 10000 (万→亿)
        try:
            total_share = round(float(mktcap) / float(price) / 10000, 4) if pd.notna(mktcap) else None
            float_share = round(float(nmc) / float(price) / 10000, 4) if pd.notna(nmc) else None
        except (ValueError, TypeError):
            continue

        rows.append((total_share, float_share, code))

    # 用新连接写入（旧连接可能已超时）
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()
    try:
        sql = """
            UPDATE market_data.stock_info AS t SET
                total_share = COALESCE(t.total_share, CAST(v.total_share AS numeric)),
                float_share = COALESCE(t.float_share, CAST(v.float_share AS numeric)),
                updated_at = CURRENT_TIMESTAMP
            FROM (VALUES %s) AS v(total_share, float_share, symbol)
            WHERE t.symbol = v.symbol
        """
        execute_values(cur, sql, rows, page_size=200)
        print(f"沪市股本反算更新：{len(rows)} 条")
    finally:
        cur.close()
        conn.close()


def show_stats(cur):
    cur.execute("SELECT COUNT(*) FROM market_data.stock_info")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM market_data.stock_info WHERE list_date IS NOT NULL")
    has_date = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM market_data.stock_info WHERE total_share IS NOT NULL")
    has_share = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM market_data.stock_info WHERE float_share IS NOT NULL")
    has_float = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM market_data.stock_info WHERE industry IS NOT NULL")
    has_ind = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM market_data.stock_info WHERE concept IS NOT NULL")
    has_concept = cur.fetchone()[0]

    print(f"\n===== 补全结果 =====")
    print(f"总计: {total} 只")
    print(f"上市日期: {has_date} ({has_date*100//total}%)")
    print(f"总股本:   {has_share} ({has_share*100//total}%)")
    print(f"流通股本: {has_float} ({has_float*100//total}%)")
    print(f"行业:     {has_ind} ({has_ind*100//total}%)")
    print(f"概念:     {has_concept} ({has_concept*100//total}%)")


def main():
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()

    try:
        fill_sh_list_date(cur)
        fill_sz_info(cur)
    finally:
        cur.close()
        conn.close()

    # 沪市股本反算（内部自行管理连接）
    fill_sh_shares(None)

    # 统计
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()
    try:
        show_stats(cur)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
