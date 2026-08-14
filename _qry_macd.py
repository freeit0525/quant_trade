# -*- coding: utf-8 -*-
"""查若干股票最新 MACD 状态，确认多头/空头环境"""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from database.db import db_cursor

syms = ["000636", "600000", "300058", "002407", "601021", "000001"]
with db_cursor() as cur:
    for s in syms:
        cur.execute("""
            SELECT trade_date, close, macd_dif, macd_dea, macd_hist
            FROM market_data.daily_kline
            WHERE symbol=%s AND macd_dif IS NOT NULL
            ORDER BY trade_date DESC LIMIT 3
        """, (s,))
        rows = cur.fetchall()
        if not rows:
            print(f"{s}: 无MACD数据"); continue
        print(f"== {s} ==")
        for d, c, dif, dea, hist in rows:
            state = "多头" if dif > dea else "空头"
            water = "水上" if dif > 0 else "水下"
            print(f"   {d}  收盘{c}  DIF {float(dif):.4f}  DEA {float(dea):.4f}  柱 {float(hist):.4f}  [{state}/{water}]")
