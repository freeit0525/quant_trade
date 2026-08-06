# -*- coding: utf-8 -*-
"""临时调试：对比库内价与 baostock 前复权/不复权价，确认口径"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import baostock as bs
import psycopg2
from config.settings import Settings

cfg = Settings().database
conn = psycopg2.connect(host=cfg.host, port=cfg.port, dbname=cfg.dbname,
                        user=cfg.user, password=cfg.password)
cur = conn.cursor()

lg = bs.login()
print("login:", lg.error_code, lg.error_msg)

cases = [
    ("sh.600000", "2024-08-05", "2024-08-06"),
    ("sh.600000", "2024-06-14", "2024-06-17"),
    ("sz.000001", "2026-02-05", "2026-02-06"),
    ("sz.300058", "2026-04-07", "2026-04-08"),
]
for code, d0, d1 in cases:
    symbol = code.split(".")[1]
    cur.execute("SELECT to_char(trade_date,'YYYY-MM-DD'), close FROM market_data.daily_kline "
                "WHERE symbol=%s AND trade_date BETWEEN %s AND %s ORDER BY trade_date",
                (symbol, d0, d1))
    db_rows = cur.fetchall()
    print(f"\n=== {code} 库内 ===")
    for r in db_rows:
        print("  ", r)
    for flag, name in [("2", "前复权"), ("3", "不复权")]:
        rs = bs.query_history_k_data_plus(
            code, "date,close,preclose", start_date=d0, end_date=d1,
            frequency="d", adjustflag=flag,
        )
        print(f"--- baostock {name} ---")
        while rs.next():
            print("  ", rs.get_row_data())

bs.logout()
cur.close(); conn.close()
