# -*- coding: utf-8 -*-
"""回填 daily_kline 中缺失的字段数值

现状缺失（722 行总量）：
  1. pct_change/change/amplitude 各缺 4 行
     库内数据为腾讯 qfq 前复权口径（实测腾讯价与库内价一致，baostock 前复权因子不同不可用）
     - 600000@2024-08-06：库内有前收盘，直接推算
     - 000001@2026-02-06 / 300058@2026-04-08 / 600000@2024-06-17：各股票库内首行，
       用腾讯 qfq 前一日收盘推算（同源同口径）
  2. 资金流 5 列缺 1 行（600000@2024-07-16）：新浪源本身缺该日，尝试东财直连补拉

用法: python scripts/backfill_missing_fields.py
幂等：只更新缺失字段（WHERE 保护），可重复执行。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import requests

from database.db import get_connection


def tencent_prev_close(symbol: str, trade_date: str) -> float | None:
    """腾讯 qfq 拉取 trade_date 前一交易日收盘价（与库内同口径），失败返回 None"""
    market = "sh" if symbol.startswith(("6", "9")) else "sz"
    qq = f"{market}{symbol}"
    d = pd.to_datetime(trade_date)
    d0 = (d - pd.Timedelta(days=15)).strftime("%Y-%m-%d")
    d1 = d.strftime("%Y-%m-%d")
    try:
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        resp = requests.get(
            url,
            params={"param": f"{qq},day,{d0},{d1},640,qfq"},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {}).get(qq, {})
        klines = data.get("qfqday") or data.get("day")
        if not klines:
            return None
        # 找到 trade_date 之前最近的交易日收盘
        prev_close = None
        target = pd.Timestamp(trade_date)
        for k in klines:
            dt = pd.to_datetime(k[0])
            if dt < target:
                prev_close = float(k[2])  # [日期,开,收,高,低,量]
        return prev_close
    except Exception as e:
        print(f"    腾讯接口异常 {qq}: {e}")
        return None


def backfill_pct_fields(cur):
    """回填 pct_change/change/amplitude（覆盖重算，幂等）

    库内为腾讯 qfq 前复权口径，前收盘取库内前一行，缺则腾讯 qfq 前一日收盘。
    """
    # 已知缺失日期（曾以错误口径写入过，直接覆盖）
    cur.execute(
        "SELECT symbol, trade_date, open, high, low, close "
        "FROM market_data.daily_kline "
        "WHERE (symbol='000001' AND trade_date='2026-02-06') "
        "   OR (symbol='300058' AND trade_date='2026-04-08') "
        "   OR (symbol='600000' AND trade_date='2024-06-17') "
        "   OR (symbol='600000' AND trade_date='2024-08-06') "
        "ORDER BY symbol, trade_date"
    )
    rows = cur.fetchall()
    if not rows:
        print("pct 类无目标行")
        return
    print(f"pct 类目标 {len(rows)} 行，开始回填...")

    for symbol, d, open_p, high, low, close in rows:
        date_str = d.strftime("%Y-%m-%d")
        # 优先用库内前收盘（同源），否则腾讯 qfq 前一日收盘
        cur.execute(
            "SELECT close FROM market_data.daily_kline WHERE symbol=%s AND trade_date < %s "
            "ORDER BY trade_date DESC LIMIT 1",
            (symbol, d),
        )
        row = cur.fetchone()
        prev_close = float(row[0]) if row else tencent_prev_close(symbol, date_str)
        if prev_close is None or prev_close <= 0:
            print(f"  !! {symbol} {date_str} 无前收盘，跳过")
            continue
        close = float(close)
        high = float(high)
        low = float(low)
        change = close - prev_close
        pct = change / prev_close * 100
        amp = (high - low) / prev_close * 100
        cur.execute(
            """UPDATE market_data.daily_kline
               SET pct_change = %s, change = %s, amplitude = %s
               WHERE symbol = %s AND trade_date = %s""",
            (round(pct, 4), round(change, 4), round(amp, 4), symbol, d),
        )
        print(f"  {symbol} {date_str}: close={close} prev_close={prev_close} "
              f"pct={pct:.4f} change={change:.4f} amplitude={amp:.4f}")


def backfill_fund_flow(cur):
    """回填资金流缺失行：新浪缺该日，尝试东财直连拉取该日分单净流入"""
    cur.execute(
        "SELECT symbol, trade_date FROM market_data.daily_kline "
        "WHERE main_net_inflow IS NULL ORDER BY symbol, trade_date"
    )
    rows = cur.fetchall()
    if not rows:
        print("资金流无缺失")
        return
    print(f"资金流缺失 {len(rows)} 行，尝试东财直连回填...")

    from data.fetcher import _em_get_with_retry

    flow_cols = [
        ("main_net_inflow", "主力"),
        ("super_large_net_inflow", "超大单"),
        ("large_net_inflow", "大单"),
        ("medium_net_inflow", "中单"),
        ("small_net_inflow", "小单"),
    ]
    for symbol, d in rows:
        date_str = d.strftime("%Y-%m-%d")
        # 东财个股资金流历史接口（push2his）
        url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
        try:
            resp = _em_get_with_retry(url, {
                "lmt": "0", "klt": "101", "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                "secid": ("1." if symbol.startswith(("6", "9")) else "0.") + symbol,
            }, retries=2)
            data = resp.json()
            klines = data.get("data", {}).get("klines") or []
            for line in klines:
                parts = line.split(",")
                if parts[0] != date_str:
                    continue
                # 东财字段: 日期,主力净流入,小单,中单,大单,超大单,主力净占比,...
                values = [float(x) if x not in ("", "-") else None for x in parts[1:6]]
                main_, small_, medium_, large_, super_ = values
                cur.execute(
                    """UPDATE market_data.daily_kline
                       SET main_net_inflow=%s, super_large_net_inflow=%s,
                           large_net_inflow=%s, medium_net_inflow=%s, small_net_inflow=%s
                       WHERE symbol=%s AND trade_date=%s""",
                    (main_, super_, large_, medium_, small_, symbol, d),
                )
                print(f"  {symbol} {date_str}: 主力={main_} 超大={super_} 大={large_} "
                      f"中={medium_} 小={small_}")
                break
            else:
                print(f"  !! 东财无 {symbol} {date_str} 资金流记录")
        except Exception as e:
            print(f"  !! 东财资金流拉取失败 {symbol}: {e}")


def main():
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()
    try:
        backfill_pct_fields(cur)
        backfill_fund_flow(cur)
    finally:
        cur.close()
        conn.close()
    print("完成")


if __name__ == "__main__":
    main()
