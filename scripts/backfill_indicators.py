# -*- coding: utf-8 -*-
"""回填 daily_kline 中历史数据的技术指标（MACD/MA/RSI/KDJ）

用法: python scripts/backfill_indicators.py
幂等：对每只股票按 trade_date 升序全量重算并 UPDATE，可重复执行。
失真补全策略：前期窗口不足时用可用窗口(min_periods=1)或中性值(RSI/KDJ 补 50)，
MACD 的 EMA 自首日全量累积，所有指标从首根起即有值。
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database.db import get_connection
from data.fetcher import MACD_GROUPS
from psycopg2.extras import execute_values


def calc_indicators(close: pd.Series, high: pd.Series, low: pd.Series,
                    volume: pd.Series) -> dict:
    """计算全部指标，返回 {列名: pd.Series}（与 daily_kline 列一一对应）"""
    out = {}

    # 量比 = 当日量 / 过去5个交易日（不含当日）均量；前期窗口不足时失真补全
    # （第1天=当日/当日=1，第2天=当日/前1日均量……第6天起为标准5日均量）
    past_avg = volume.rolling(5, min_periods=1).mean().shift(1)
    out["volume_ratio"] = volume / past_avg.fillna(volume)

    # MACD：多组参数（默认 12/26/9 + 6/12/5 + 3/8/3 + 10/20/7），EMA 自首日全量累积
    for suffix, (fast, slow, signal) in MACD_GROUPS.items():
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        dif = ema_fast - ema_slow
        dea = dif.ewm(span=signal, adjust=False).mean()
        out[f"macd_dif{suffix}"], out[f"macd_dea{suffix}"], out[f"macd_hist{suffix}"] = dif, dea, (dif - dea) * 2

    # MA：简单移动平均（前期窗口不足时用可用窗口，min_periods=1 失真补全）
    for n in (5, 10, 20, 30, 60, 120):
        out[f"ma{n}"] = close.rolling(n, min_periods=1).mean()

    # RSI(14) Wilder 平滑（首根及完全横盘无涨跌时失真补 50）
    period = 14
    delta = close.diff().fillna(0)
    avg_gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    out["rsi14"] = (100 - 100 / (1 + rs)).fillna(50)

    # KDJ(9,3,3)：前期窗口不足时用可用窗口(min_periods=1)；HH=LL 无波动时 RSV 补 50
    low9 = low.rolling(9, min_periods=1).min()
    high9 = high.rolling(9, min_periods=1).max()
    rsv = ((close - low9) / (high9 - low9) * 100).fillna(50)
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    d = k.ewm(alpha=1 / 3, adjust=False).mean()
    out["kdj_k"], out["kdj_d"], out["kdj_j"] = k, d, 3 * k - 2 * d

    # BIAS(5,10,20)：乖离率 = (close - MA(n)) / MA(n) * 100（MA 用可用窗口）
    for n in (5, 10, 20):
        ma_n = close.rolling(n, min_periods=1).mean()
        out[f"bias{n}"] = (close - ma_n) / ma_n * 100
    return out


# 全部指标列（失真补全策略下从首根起即有值）
INDICATOR_COLS = (
    ["volume_ratio"]
    + [f"ma{n}" for n in (5, 10, 20, 30, 60, 120)]
    + ["rsi14", "kdj_k", "kdj_d", "kdj_j"]
    + [f"bias{n}" for n in (5, 10, 20)]
    + [c for s in MACD_GROUPS for c in (f"macd_dif{s}", f"macd_dea{s}", f"macd_hist{s}")]
)


def backfill_indicators():
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("SELECT DISTINCT symbol FROM market_data.daily_kline ORDER BY symbol")
    symbols = [r[0] for r in cur.fetchall()]
    total = len(symbols)
    print(f"共 {total} 只股票需要回填技术指标")

    updated = 0
    for i, symbol in enumerate(symbols, 1):
        cur.execute(
            "SELECT trade_date, open, high, low, close, volume FROM market_data.daily_kline "
            "WHERE symbol = %s ORDER BY trade_date",
            (symbol,),
        )
        rows = cur.fetchall()
        if len(rows) < 9:
            continue
        dates = [r[0] for r in rows]
        close = pd.Series([float(r[4]) for r in rows], dtype="float64")
        high = pd.Series([float(r[2]) for r in rows], dtype="float64")
        low = pd.Series([float(r[3]) for r in rows], dtype="float64")
        volume = pd.Series([float(r[5]) for r in rows], dtype="float64")
        ind = calc_indicators(close, high, low, volume)

        data = []
        for j, d in enumerate(dates):
            record = [symbol, d]
            for col in INDICATOR_COLS:
                v = ind[col].iloc[j]
                record.append(None if pd.isna(v) else float(v))
            data.append(record)

        cols = ", ".join(INDICATOR_COLS)
        # 自定义 template 给每列显式 cast：避免全 NULL 列被推断为 text
        template = (
            "(%s::text, %s::date, "
            + ", ".join(["%s::numeric"] * len(INDICATOR_COLS))
            + ")"
        )
        execute_values(
            cur,
            f"""UPDATE market_data.daily_kline AS t
                SET {", ".join(f"{c} = e.{c}" for c in INDICATOR_COLS)}
                FROM (VALUES %s) AS e(symbol, trade_date, {cols})
                WHERE t.symbol = e.symbol AND t.trade_date = e.trade_date""",
            data,
            template=template,
            page_size=5000,
        )
        updated += len(data)
        if i % 200 == 0 or i == total:
            print(f"  进度: {i}/{total}（累计更新 {updated} 行）", flush=True)

    cur.close()
    conn.close()
    print(f"回填完成：{total} 只股票，更新 {updated} 行")


if __name__ == "__main__":
    backfill_indicators()
