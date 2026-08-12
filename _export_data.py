# -*- coding: utf-8 -*-
# 临时脚本：导出 50 只全池 + 沪深300 指数到 _kline_data.json（供 Node 因子实验）
import json
import math
from datetime import datetime, date

import pandas as pd

from database.db import db_cursor, get_kline

OUT = '_kline_data.json'


def clean(obj):
    """把 NaN/Infinity 转 None、Timestamp 转 'YYYY-MM-DD'，保证 JSON 合法"""
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean(x) for x in obj]
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        return obj.strftime('%Y-%m-%d')
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def main():
    # 股票池：直接取库内有 K 线数据的股票（即当前全池），排除指数
    with db_cursor() as cur:
        cur.execute("SELECT DISTINCT symbol FROM market_data.daily_kline ORDER BY symbol")
        symbols = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT symbol, name FROM market_data.stock_info")
        names = dict(cur.fetchall())
    stocks = []
    for symbol in symbols:
        if symbol.startswith('sh.000') or symbol.startswith('sz.399') or symbol.startswith('bj.8'):
            continue
        name = names.get(symbol, symbol)
        df = get_kline(symbol, '1990-01-01', '2099-12-31')
        if df is None or df.empty:
            print(f'skip {symbol} ({name}) empty')
            continue
        rows = clean(df.to_dict('records'))
        stocks.append({'symbol': symbol, 'name': name, 'rows': rows})
        print(f'{symbol} {name}: {len(rows)} rows')

    # 沪深300 指数（市场过滤用）
    idx = None
    idf = get_kline('sh.000300', '1990-01-01', '2099-12-31')
    if idf is not None and not idf.empty:
        idx = {'symbol': 'sh.000300', 'rows': clean(idf.to_dict('records'))}
        print(f'index sh.000300: {len(idf)} rows')
    else:
        print('WARN: sh.000300 empty')

    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump({'stocks': stocks, 'index': idx}, f, ensure_ascii=False)
    print(f'saved {OUT}: {len(stocks)} stocks')


if __name__ == '__main__':
    main()
