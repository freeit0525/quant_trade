# -*- coding: utf-8 -*-
"""临时调试：东财 stock_zh_a_hist 能否拉到缺失日"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import akshare as ak
import pandas as pd

pd.set_option("display.width", 200)
for symbol, d0, d1 in [
    ("000001", "2026-02-05", "2026-02-06"),
    ("300058", "2026-04-07", "2026-04-08"),
    ("600000", "2024-06-16", "2024-06-17"),
    ("600000", "2024-08-05", "2024-08-06"),
]:
    try:
        df = ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                start_date=d0.replace("-", ""), end_date=d1.replace("-", ""),
                                adjust="qfq")
        print(f"=== {symbol} {d0}~{d1} ===")
        print(df.to_string())
    except Exception as e:
        print(f"=== {symbol} 失败: {type(e).__name__} {e} ===")
