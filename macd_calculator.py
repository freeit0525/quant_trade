"""
MACD计算工具 - 通过历史数据计算未来价格区间内的MACD值

功能：
1. 基于历史收盘价计算MACD指标
2. 可调整MACD参数（fast_period, slow_period, signal_period）
3. 输入明日预测价格，计算该价格下的MACD值
"""

import pandas as pd


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    """计算EMA"""
    return series.ewm(span=period, adjust=False).mean()


def calc_macd(
    closes: list[float] | pd.Series,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    计算MACD指标

    参数:
        closes: 收盘价序列
        fast_period: 快线EMA周期，默认12
        slow_period: 慢线EMA周期，默认26
        signal_period: 信号线EMA周期，默认9

    返回:
        (DIF, DEA, MACD柱)
    """
    closes = pd.Series(closes, dtype=float)
    ema_fast = calc_ema(closes, fast_period)
    ema_slow = calc_ema(closes, slow_period)
    dif = ema_fast - ema_slow
    dea = calc_ema(dif, signal_period)
    macd_bar = (dif - dea) * 2
    return dif, dea, macd_bar


def predict_macd(
    history_closes: list[float],
    tomorrow_price: float,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> dict:
    """
    输入明日预测价格，计算明日MACD值

    参数:
        history_closes: 历史收盘价列表
        tomorrow_price: 明日预测价格
        fast_period: 快线周期
        slow_period: 慢线周期
        signal_period: 信号线周期

    返回:
        dict: 包含今日和明日的DIF/DEA/MACD值
    """
    if len(history_closes) < slow_period:
        raise ValueError(f"历史数据至少需要{slow_period}条，当前只有{len(history_closes)}条")

    # 用历史数据计算到今天
    dif, dea, macd_bar = calc_macd(history_closes, fast_period, slow_period, signal_period)

    today_dif = dif.iloc[-1]
    today_dea = dea.iloc[-1]
    today_macd = macd_bar.iloc[-1]

    # 加入明日价格重新计算
    extended_closes = list(history_closes) + [tomorrow_price]
    dif2, dea2, macd_bar2 = calc_macd(extended_closes, fast_period, slow_period, signal_period)

    tomorrow_dif = dif2.iloc[-1]
    tomorrow_dea = dea2.iloc[-1]
    tomorrow_macd = macd_bar2.iloc[-1]

    return {
        "today": {"dif": round(today_dif, 4), "dea": round(today_dea, 4), "macd": round(today_macd, 4)},
        "tomorrow": {"dif": round(tomorrow_dif, 4), "dea": round(tomorrow_dea, 4), "macd": round(tomorrow_macd, 4)},
    }


def predict_macd_range(
    history_closes: list[float],
    price_range: tuple[float, float],
    step: float = 0.01,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> pd.DataFrame:
    """
    计算价格区间内每个价格对应的MACD值

    参数:
        history_closes: 历史收盘价列表
        price_range: (最低价, 最高价)
        step: 价格步长
        fast_period/slow_period/signal_period: MACD参数

    返回:
        DataFrame: 包含price, dif, dea, macd列
    """
    low, high = price_range
    prices = []
    difs = []
    deas = []
    macds = []

    price = low
    while price <= high:
        result = predict_macd(history_closes, price, fast_period, slow_period, signal_period)
        prices.append(price)
        difs.append(result["tomorrow"]["dif"])
        deas.append(result["tomorrow"]["dea"])
        macds.append(result["tomorrow"]["macd"])
        price = round(price + step, 4)

    return pd.DataFrame({"price": prices, "dif": difs, "dea": deas, "macd": macds})


if __name__ == "__main__":
    # 示例：使用模拟历史数据
    import random

    random.seed(42)
    history = [100 + random.uniform(-2, 2) for _ in range(50)]

    print("=" * 60)
    print("MACD计算工具 - 示例")
    print("=" * 60)
    print(f"\n历史数据条数: {len(history)}")
    print(f"最新收盘价: {history[-1]}")

    # 示例1：输入明日价格计算MACD
    tomorrow_price = 101.5
    result = predict_macd(history, tomorrow_price)
    print(f"\n明日预测价格: {tomorrow_price}")
    print(f"  今日  -> DIF: {result['today']['dif']}, DEA: {result['today']['dea']}, MACD: {result['today']['macd']}")
    print(f"  明日  -> DIF: {result['tomorrow']['dif']}, DEA: {result['tomorrow']['dea']}, MACD: {result['tomorrow']['macd']}")

    # 示例2：自定义MACD参数
    result2 = predict_macd(history, tomorrow_price, fast_period=6, slow_period=12, signal_period=5)
    print(f"\n自定义参数(6,12,5) 明日价格: {tomorrow_price}")
    print(f"  明日  -> DIF: {result2['tomorrow']['dif']}, DEA: {result2['tomorrow']['dea']}, MACD: {result2['tomorrow']['macd']}")

    # 示例3：价格区间计算（只显示关键行）
    print(f"\n价格区间计算 (99.5 ~ 102.0, 步长0.5):")
    df = predict_macd_range(history, (99.5, 102.0), step=0.5)
    print(df.to_string(index=False))
