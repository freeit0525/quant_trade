# -*- coding: utf-8 -*-
"""临时调试：新浪资金流 600000 2024-07 数据"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
import pandas as pd

url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_lscjfb"
resp = requests.get(
    url,
    params={"page": 1, "num": 2000, "sort": "opendate", "asc": 0, "daima": "sh600000"},
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
             "Referer": "https://finance.sina.com.cn/"},
    timeout=15,
)
resp.raise_for_status()
data = resp.json()
print(f"共 {len(data)} 条，日期范围: {data[0]['opendate']} ~ {data[-1]['opendate']}")

# 找 2024-07-16 附近
for row in data:
    if "2024-07" in row["opendate"]:
        print(row["opendate"], row["r0_net"], row["r1_net"], row["r2_net"], row["r3_net"])

# 检查 2024-07-16 是否存在
dates = [r["opendate"] for r in data]
print("2024-07-16 在列:", "2024-07-16" in dates)
