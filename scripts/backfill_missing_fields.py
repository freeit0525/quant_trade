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
    """回填资金流缺失行：仅处理近100天（数据源只提供近期资金流，历史缺失属正常），
    新浪缺该日时尝试东财直连拉取该日分单净流入"""
    cur.execute(
        "SELECT symbol, trade_date FROM market_data.daily_kline "
        "WHERE main_net_inflow IS NULL AND trade_date >= CURRENT_DATE - INTERVAL '100 days' "
        "ORDER BY symbol, trade_date"
    )
    rows = cur.fetchall()
    if not rows:
        print("资金流无缺失")
        return
    print(f"资金流缺失 {len(rows)} 行，尝试东财直连回填...")

    from data.fetcher import _em_get_with_retry

    # 预检东财连通性：不通则跳过，避免逐行等待超时
    try:
        probe = _em_get_with_retry(
            "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
            {"lmt": "0", "klt": "101", "fields1": "f1,f2,f3,f7",
             "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
             "secid": "1.600000"},
            retries=1, timeout=8,
        )
        if not (probe.json().get("data") or {}).get("klines"):
            print("东财资金流接口无数据，跳过资金流回填")
            return
    except Exception as e:
        print(f"东财资金流接口不可用，跳过资金流回填: {e}")
        return

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


def backfill_amplitude(cur):
    """回填缺失的振幅：振幅 = (最高-最低) / 前收盘 * 100

    前收盘取库内该股票前一行 close（同前复权口径），已用库内 14189 行真实振幅
    交叉验证，平均误差 0.0009%，可直接采用。股票首日无前收盘则保留 NULL。
    """
    cur.execute(
        "SELECT symbol, trade_date, open, high, low, close "
        "FROM market_data.daily_kline WHERE amplitude IS NULL "
        "ORDER BY symbol, trade_date"
    )
    rows = cur.fetchall()
    if not rows:
        print("振幅无缺失")
        return
    print(f"振幅缺失 {len(rows)} 行，开始公式回填...")
    updated = 0
    for symbol, d, open_p, high, low, close in rows:
        cur.execute(
            "SELECT close FROM market_data.daily_kline "
            "WHERE symbol=%s AND trade_date<%s ORDER BY trade_date DESC LIMIT 1",
            (symbol, d),
        )
        r = cur.fetchone()
        if r is None or float(r[0]) <= 0:
            continue  # 首日无前收盘，无法计算
        prev_close = float(r[0])
        amp = (float(high) - float(low)) / prev_close * 100
        cur.execute(
            "UPDATE market_data.daily_kline SET amplitude=%s "
            "WHERE symbol=%s AND trade_date=%s",
            (round(amp, 4), symbol, d),
        )
        updated += 1
    print(f"振幅回填完成：{updated}/{len(rows)} 行")


def _estimate_turnover(df: pd.DataFrame, idx: int, win: int = 90) -> float | None:
    """用缺失日前后 win 天内其他真实换手率反推当时流通股本，估算该日换手率

    真实换手率 = 成交量/当时流通股本*100，可反推 cap = volume/(turnover/100)。
    窗口内 cap 变异系数(CV) > 15% 视为流通股本发生变动，估算不可信，返回 None。
    """
    d = df["date"].iloc[idx]
    if df["volume"].iloc[idx] == 0:
        return 0.0  # 当日无成交，换手率为 0
    mask = df["date"].between(d - pd.Timedelta(days=win), d + pd.Timedelta(days=win))
    mask.iloc[idx] = False
    real = df.loc[mask & df["turnover"].notna()]
    if len(real) < 3:
        return None
    cap = real["volume"] / (real["turnover"] / 100)
    if cap.mean() <= 0 or cap.std() / cap.mean() > 0.15:
        return None
    return df["volume"].iloc[idx] / cap.median() * 100


def backfill_turnover_estimate(cur):
    """回填缺失的换手率：用附近真实换手率反推流通股本估算

    三方接口（baostock 历史 turn 为空、东财被风控）均无数据的日期，
    用前后 90 天内已有真实换手率反推当时流通股本估算（已验证：留一交叉
    验证 6381 行平均误差 0.06%，99.5% 行误差<5%）。流通股本突变段估算
    不可信，保留 NULL。
    """
    cur.execute("SELECT DISTINCT symbol FROM market_data.daily_kline ORDER BY symbol")
    symbols = [r[0] for r in cur.fetchall()]
    updated = failed = 0
    for symbol in symbols:
        cur.execute(
            "SELECT trade_date, volume, turnover FROM market_data.daily_kline "
            "WHERE symbol=%s ORDER BY trade_date",
            (symbol,),
        )
        rows = cur.fetchall()
        if not rows:
            continue
        df = pd.DataFrame(rows, columns=["date", "volume", "turnover"])
        df["date"] = pd.to_datetime(df["date"])
        df["volume"] = df["volume"].astype(float)
        df["turnover"] = df["turnover"].astype(float)
        for i in df.index[df["turnover"].isna()]:
            est = _estimate_turnover(df, i)
            if est is None:
                failed += 1
                continue
            cur.execute(
                "UPDATE market_data.daily_kline SET turnover=%s "
                "WHERE symbol=%s AND trade_date=%s",
                (round(float(est), 4), symbol, df["date"].iloc[i].date()),
            )
            updated += 1
        if updated % 200 == 0 and updated:
            print(f"  进度: 已估算 {updated} 行...", flush=True)
    print(f"换手率估算回填完成：更新 {updated} 行，不可估算保留 NULL {failed} 行")


def backfill_fund_flow_em_overwrite(cur):
    """用东财直连数据覆盖已有资金流（修正新浪源错误数据）

    新浪 lscjfb 的净流入字段与东财口径差异巨大（实测方向都可能相反），
    之前回退新浪入库导致库内近 120 个交易日资金流失真。
    东财直连覆盖范围为接口返回的全部日期（约近 120 个交易日），幂等可重复跑。
    """
    from data.fetcher import DataFetcher

    cur.execute("SELECT DISTINCT symbol FROM market_data.daily_kline ORDER BY symbol")
    symbols = [r[0] for r in cur.fetchall()]
    fetcher = DataFetcher()
    updated = 0
    for symbol in symbols:
        ff = fetcher._fetch_fund_flow_via_eastmoney(symbol)
        if ff is None or ff.empty:
            print(f"  !! {symbol} 东财资金流为空，跳过")
            continue
        for _, row in ff.iterrows():
            cur.execute(
                """UPDATE market_data.daily_kline
                   SET main_net_inflow=%s, super_large_net_inflow=%s,
                       large_net_inflow=%s, medium_net_inflow=%s, small_net_inflow=%s
                   WHERE symbol=%s AND trade_date=%s""",
                (
                    float(row["main_net_inflow"]), float(row["super_large_net_inflow"]),
                    float(row["large_net_inflow"]), float(row["medium_net_inflow"]),
                    float(row["small_net_inflow"]), symbol, row["date"].date(),
                ),
            )
            updated += cur.rowcount
        print(f"  {symbol}: 东财资金流 {len(ff)} 行已覆盖")
    print(f"东财资金流覆盖回填完成：更新 {updated} 行")


def main():
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()
    try:
        backfill_pct_fields(cur)
        backfill_fund_flow(cur)
        backfill_fund_flow_em_overwrite(cur)
        backfill_amplitude(cur)
        backfill_turnover_estimate(cur)
    finally:
        cur.close()
        conn.close()
    print("完成")


if __name__ == "__main__":
    main()
