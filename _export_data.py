# -*- coding: utf-8 -*-
"""临时脚本：从库导出全部股票K线与info到 JSON，供 Node 信号质量实验使用。用完删除。"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from database.db import db_cursor, get_kline

COLS = [
    "date", "open", "high", "low", "close", "volume", "amount", "turnover",
    "pct_change", "volume_ratio", "main_net_inflow",
    "macd_dif", "macd_dea", "macd_hist",
    "ma5", "ma10", "ma20", "ma30",
    "rsi6", "rsi14", "kdj_k", "kdj_d", "kdj_j",
    "bias5", "bias10", "bias20",
]


def main():
    with db_cursor() as cur:
        cur.execute("SELECT DISTINCT symbol FROM market_data.daily_kline ORDER BY symbol")
        syms = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT symbol, name, list_date, float_market_cap, total_market_cap FROM market_data.stock_info WHERE symbol = ANY(%s) ORDER BY symbol",
                    (syms,))
        infos = cur.fetchall()
    out = []
    for symbol, name, list_date, float_cap, total_cap in infos:
        df = get_kline(symbol, "19000101", "20991231")
        if df.empty:
            continue
        df = df[COLS]
        df = df.where(df.notna(), None)
        rows = []
        for _, r in df.iterrows():
            row = {}
            for c in COLS:
                val = r[c]
                if hasattr(val, "item"):
                    val = val.item()
                if c == "date":
                    row[c] = val.strftime("%Y-%m-%d") if hasattr(val, "strftime") else str(val)
                else:
                    row[c] = None if val is None else float(val)
            rows.append(row)
        out.append({
            "symbol": symbol,
            "name": name,
            "list_date": list_date.strftime("%Y-%m-%d") if hasattr(list_date, "strftime") else list_date,
            "float_market_cap": float(float_cap) if float_cap is not None else None,
            "total_market_cap": float(total_cap) if total_cap is not None else None,
            "rows": rows,
        })
        print(f"{symbol} {name} {len(rows)} rows")
    with open("_kline_data.json", "w", encoding="utf-8") as f:
        json.dump({"stocks": out}, f, ensure_ascii=False)
    print(f"total {len(out)} stocks -> _kline_data.json")


if __name__ == "__main__":
    main()
