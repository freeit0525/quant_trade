# -*- coding: utf-8 -*-
"""临时调试：腾讯资金流接口"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

# 腾讯个股资金流历史接口
urls = [
    ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/dayzjlx/get",
     {"code": "sh600000", "day": "2024-07-16", "type": "day"}),
    ("https://web.ifzq.gtimg.cn/appstock/app/dayzjlx/get",
     {"code": "sh600000", "day": "2024-07-16"}),
    ("https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList",
     {"board_code": "aStock", "sort_type": "1", "direct": "0", "offset": "0", "count": "1"}),
]
for url, params in urls:
    try:
        resp = requests.get(url, params=params,
                            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
                            timeout=10)
        print(f"=== {url.split('/')[-1]} status={resp.status_code} ===")
        print(resp.text[:600])
    except Exception as e:
        print(f"=== {url.split('/')[-1]} 失败: {e} ===")
