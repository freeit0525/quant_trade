# -*- coding: utf-8 -*-
"""临时调试：腾讯 qfq 拉取缺失日附近价格，对比库内"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
import pandas as pd
from database.db import get_connection

cases = [
    ("000001", "2026-02-03", "2026-02-06"),
    ("300058", "2026-04-06", "2026-04-08"),
    ("600000", "2024-06-14", "2024-06-17"),
    ("600000", "2024-08-05", "2024-08-06"),
]
for symbol, d0, d1 in cases:
    market = "sh" if symbol.startswith(("6", "9")) else "sz"
    qq = f"{market}{symbol}"
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    resp = requests.get(url, params={"param": f"{qq},day,{d0},{d1},640,qfq"},
                        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}, timeout=15)
    data = resp.json().get("data", {}).get(qq, {})
    klines = data.get("qfqday") or data.get("day")
    print(f"\n=== {symbol} 腾讯 qfq {d0}~{d1} ===")
    for k in (klines or []):
        print("  ", k)

# 对比库内
conn = get_connection(); cur = conn.cursor()
for symbol, d0, d1 in cases:
    cur.execute("SELECT to_char(trade_date,'YYYY-MM-DD'), close FROM market_data.daily_kline "
                "WHERE symbol=%s AND trade_date BETWEEN %s AND %s ORDER BY trade_date", (symbol, d0, d1))
    print(f"--- 库内 {symbol} ---")
    for r in cur.fetchall():
        print("  ", r)
cur.close(); conn.close()
