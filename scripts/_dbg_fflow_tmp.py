# -*- coding: utf-8 -*-
"""临时调试：东财 delay 主机拉全量资金流"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curl_cffi import requests as cr

for host in ["push2delay.eastmoney.com", "push2his.eastmoney.com"]:
    url = f"https://{host}/api/qt/stock/fflow/daykline/get"
    for lmt in [0, 3000, 800]:
        try:
            resp = cr.get(url, params={
                "lmt": str(lmt), "klt": "101", "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                "secid": "1.600000", "ut": "b2884a393a59ad64002292a3e90d46a5",
            }, impersonate="chrome", timeout=10)
            resp.raise_for_status()
            data = resp.json().get("data", {})
            klines = data.get("klines") or []
            hit = [k for k in klines if k.startswith("2024-07-16")]
            print(f"{host} lmt={lmt}: 总{len(klines)}条, 首={klines[0][:40] if klines else '-'}, "
                  f"尾={klines[-1][:40] if klines else '-'}, 命中07-16: {hit}")
        except Exception as e:
            print(f"{host} lmt={lmt}: 失败 {type(e).__name__}: {str(e)[:60]}")
