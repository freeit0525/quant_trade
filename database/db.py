"""数据库 CRUD 模块 - 策略、回测结果、交易记录、净值的读写"""

import math
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

from config.settings import DatabaseConfig
from utils.logger import get_logger

logger = get_logger(__name__)


def get_connection(config: DatabaseConfig | None = None):
    """获取数据库连接"""
    if config is None:
        from config.settings import Settings
        config = Settings().database
    return psycopg2.connect(
        host=config.host,
        port=config.port,
        dbname=config.dbname,
        user=config.user,
        password=config.password,
    )


@contextmanager
def db_cursor(config: DatabaseConfig | None = None):
    """数据库游标上下文管理器，自动提交和关闭"""
    conn = get_connection(config)
    try:
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    finally:
        conn.close()


# ============================================================
# 策略 CRUD
# ============================================================

def save_strategy(
    strategy_name: str,
    strategy_type: str,
    params: dict,
    is_active: bool = True,
    config: DatabaseConfig | None = None,
) -> int:
    """保存策略配置，返回策略ID"""
    import json
    with db_cursor(config) as cur:
        cur.execute(
            """INSERT INTO strategy.config (strategy_name, strategy_type, params, is_active)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (strategy_name, strategy_type, json.dumps(params, ensure_ascii=False), is_active),
        )
        strategy_id = cur.fetchone()[0]
    logger.info(f"策略已保存: {strategy_name}, id={strategy_id}")
    return strategy_id


def update_strategy(
    strategy_id: int,
    strategy_name: str | None = None,
    params: dict | None = None,
    is_active: bool | None = None,
    config: DatabaseConfig | None = None,
) -> None:
    """更新策略配置"""
    import json
    updates = []
    values = []
    if strategy_name is not None:
        updates.append("strategy_name = %s")
        values.append(strategy_name)
    if params is not None:
        updates.append("params = %s")
        values.append(json.dumps(params, ensure_ascii=False))
    if is_active is not None:
        updates.append("is_active = %s")
        values.append(is_active)
    updates.append("updated_at = CURRENT_TIMESTAMP")
    values.append(strategy_id)

    with db_cursor(config) as cur:
        cur.execute(
            f"UPDATE strategy.config SET {', '.join(updates)} WHERE id = %s",
            values,
        )
    logger.info(f"策略已更新: id={strategy_id}")


def get_strategies(
    active_only: bool = False,
    config: DatabaseConfig | None = None,
) -> list[dict]:
    """获取所有策略配置"""
    with db_cursor(config) as cur:
        sql = "SELECT id, strategy_name, strategy_type, params, is_active, created_at, updated_at FROM strategy.config"
        if active_only:
            sql += " WHERE is_active = TRUE"
        sql += " ORDER BY created_at DESC"
        cur.execute(sql)
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
    return [dict(zip(columns, row)) for row in rows]


def get_strategy_by_id(strategy_id: int, config: DatabaseConfig | None = None) -> Optional[dict]:
    """根据ID获取策略配置"""
    with db_cursor(config) as cur:
        cur.execute(
            "SELECT id, strategy_name, strategy_type, params, is_active, created_at, updated_at FROM strategy.config WHERE id = %s",
            (strategy_id,),
        )
        columns = [desc[0] for desc in cur.description]
        row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(columns, row))


def delete_strategy(strategy_id: int, config: DatabaseConfig | None = None) -> None:
    """删除策略配置"""
    with db_cursor(config) as cur:
        cur.execute("DELETE FROM strategy.config WHERE id = %s", (strategy_id,))
    logger.info(f"策略已删除: id={strategy_id}")


# ============================================================
# 回测结果 CRUD
# ============================================================

def save_backtest_result(
    strategy_name: str,
    symbol: str,
    start_date: str,
    end_date: str,
    initial_capital: float,
    final_capital: float,
    total_return: float,
    annual_return: float,
    max_drawdown: float,
    sharpe_ratio: float,
    params: dict | None = None,
    win_rate: float = 0.0,
    total_trades: int = 0,
    trading_days: int = 0,
    total_commission: float = 0.0,
    total_stamp_tax: float = 0.0,
    config: DatabaseConfig | None = None,
) -> int:
    """保存回测结果，返回结果ID"""
    import json
    with db_cursor(config) as cur:
        cur.execute(
            """INSERT INTO backtest.results
               (strategy_name, symbol, start_date, end_date, initial_capital, final_capital,
                total_return, annual_return, max_drawdown, sharpe_ratio, params)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (
                strategy_name, symbol, start_date, end_date,
                initial_capital, final_capital,
                total_return, annual_return, max_drawdown, sharpe_ratio,
                json.dumps(params, ensure_ascii=False) if params else None,
            ),
        )
        result_id = cur.fetchone()[0]
    logger.info(f"回测结果已保存: {strategy_name} {symbol}, result_id={result_id}")
    return result_id


def get_backtest_results(
    limit: int = 50,
    config: DatabaseConfig | None = None,
) -> list[dict]:
    """获取回测结果列表"""
    with db_cursor(config) as cur:
        cur.execute(
            """SELECT id, strategy_name, symbol, start_date, end_date,
                      initial_capital, final_capital, total_return, annual_return,
                      max_drawdown, sharpe_ratio, params, created_at
               FROM backtest.results ORDER BY created_at DESC LIMIT %s""",
            (limit,),
        )
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
    return [dict(zip(columns, row)) for row in rows]


def get_backtest_result_by_id(result_id: int, config: DatabaseConfig | None = None) -> Optional[dict]:
    """根据ID获取回测结果"""
    with db_cursor(config) as cur:
        cur.execute(
            """SELECT id, strategy_name, symbol, start_date, end_date,
                      initial_capital, final_capital, total_return, annual_return,
                      max_drawdown, sharpe_ratio, params, created_at
               FROM backtest.results WHERE id = %s""",
            (result_id,),
        )
        columns = [desc[0] for desc in cur.description]
        row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(columns, row))


def delete_backtest_result(result_id: int, config: DatabaseConfig | None = None) -> None:
    """删除回测结果（级联删除关联的交易和净值记录）"""
    with db_cursor(config) as cur:
        cur.execute("DELETE FROM backtest.nav WHERE result_id = %s", (result_id,))
        cur.execute("DELETE FROM backtest.trades WHERE result_id = %s", (result_id,))
        cur.execute("DELETE FROM backtest.results WHERE id = %s", (result_id,))
    logger.info(f"回测结果已删除: result_id={result_id}")


# ============================================================
# 交易记录 CRUD
# ============================================================

def save_trades(
    result_id: int,
    trades_df: pd.DataFrame,
    config: DatabaseConfig | None = None,
) -> int:
    """批量保存交易记录"""
    if trades_df.empty:
        return 0

    rows = []
    for _, row in trades_df.iterrows():
        rows.append((
            result_id,
            row["symbol"],
            row["direction"],
            row["date"] if isinstance(row["date"], str) else row["date"].strftime("%Y-%m-%d"),
            float(row["price"]),
            int(row["volume"]),
            float(row.get("commission", 0)),
        ))

    with db_cursor(config) as cur:
        execute_values(
            cur,
            """INSERT INTO backtest.trades
               (result_id, symbol, trade_type, trade_date, price, quantity, commission)
               VALUES %s""",
            rows,
        )
    logger.info(f"交易记录已保存: {len(rows)} 条, result_id={result_id}")
    return len(rows)


def get_trades_by_result_id(result_id: int, config: DatabaseConfig | None = None) -> pd.DataFrame:
    """根据回测结果ID获取交易记录"""
    with db_cursor(config) as cur:
        cur.execute(
            """SELECT symbol, trade_type as direction, trade_date as date,
                      price, quantity as volume, commission
               FROM backtest.trades WHERE result_id = %s ORDER BY trade_date""",
            (result_id,),
        )
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows, columns=columns)


# ============================================================
# 净值记录 CRUD
# ============================================================

def save_nav(
    result_id: int,
    nav_df: pd.DataFrame,
    config: DatabaseConfig | None = None,
) -> int:
    """批量保存净值记录"""
    if nav_df.empty:
        return 0

    rows = []
    for _, row in nav_df.iterrows():
        date_str = row["date"] if isinstance(row["date"], str) else row["date"].strftime("%Y-%m-%d")
        rows.append((
            result_id,
            date_str,
            float(row.get("cash", 0)),
            float(row.get("market_value", 0)),
            float(row.get("total_value", 0)),
            float(row.get("nav", 1.0)),
        ))

    with db_cursor(config) as cur:
        execute_values(
            cur,
            """INSERT INTO backtest.nav
               (result_id, nav_date, cash, market_value, total_value, nav)
               VALUES %s""",
            rows,
        )
    logger.info(f"净值记录已保存: {len(rows)} 条, result_id={result_id}")
    return len(rows)


def get_nav_by_result_id(result_id: int, config: DatabaseConfig | None = None) -> pd.DataFrame:
    """根据回测结果ID获取净值记录"""
    with db_cursor(config) as cur:
        cur.execute(
            """SELECT nav_date as date, cash, market_value, total_value, nav
               FROM backtest.nav WHERE result_id = %s ORDER BY nav_date""",
            (result_id,),
        )
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=columns)
    df["date"] = pd.to_datetime(df["date"])
    return df


# ============================================================
# 日K线 CRUD
# ============================================================

def get_kline_max_date(symbol: str, config: DatabaseConfig | None = None) -> Optional[str]:
    """查询某只股票在 daily_kline 表中已存在的最大交易日期

    Returns:
        'YYYY-MM-DD' 字符串；表中无该 symbol 数据时返回 None
    """
    with db_cursor(config) as cur:
        cur.execute(
            "SELECT MAX(trade_date) FROM market_data.daily_kline WHERE symbol = %s",
            (symbol,),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return row[0].strftime("%Y-%m-%d")


def get_kline_min_date(symbol: str, config: DatabaseConfig | None = None) -> Optional[str]:
    """查询某只股票在 daily_kline 表中已存在的最小交易日期

    Returns:
        'YYYY-MM-DD' 字符串；表中无该 symbol 数据时返回 None
    """
    with db_cursor(config) as cur:
        cur.execute(
            "SELECT MIN(trade_date) FROM market_data.daily_kline WHERE symbol = %s",
            (symbol,),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return row[0].strftime("%Y-%m-%d")


def get_stock_list_date(symbol: str, config: DatabaseConfig | None = None) -> Optional[str]:
    """查询股票上市日期（stock_info.list_date）

    Returns:
        'YYYY-MM-DD' 字符串；库中无该股票或上市日期为空时返回 None
    """
    with db_cursor(config) as cur:
        cur.execute(
            "SELECT list_date FROM market_data.stock_info WHERE symbol = %s",
            (symbol,),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return row[0].strftime("%Y-%m-%d")


def get_kline(
    symbol: str,
    start_date: str,
    end_date: str,
    config: DatabaseConfig | None = None,
) -> pd.DataFrame:
    """从数据库读取日K线数据，日期范围 [start_date, end_date]（含两端）

    Args:
        symbol: 股票代码
        start_date: 开始日期，'YYYY-MM-DD' 或 'YYYYMMDD'
        end_date: 结束日期，'YYYY-MM-DD' 或 'YYYYMMDD'

    Returns:
        包含 date/open/high/low/close/volume/amount/turnover 的 DataFrame，按日期升序
    """
    start_str = pd.to_datetime(start_date).strftime("%Y-%m-%d")
    end_str = pd.to_datetime(end_date).strftime("%Y-%m-%d")
    with db_cursor(config) as cur:
        cur.execute(
            """SELECT trade_date AS date, open, high, low, close, volume, amount, turnover,
                      pct_change, change, amplitude, volume_ratio,
                      main_net_inflow, super_large_net_inflow, large_net_inflow,
                      medium_net_inflow, small_net_inflow,
                      macd_dif, macd_dea, macd_hist,
                      ma5, ma10, ma20, ma30, ma60, ma120,
                      rsi14, kdj_k, kdj_d, kdj_j,
                      bias5, bias10, bias20,
                      macd_dif_6_12_5, macd_dea_6_12_5, macd_hist_6_12_5,
                      macd_dif_3_8_3, macd_dea_3_8_3, macd_hist_3_8_3,
                      macd_dif_10_20_7, macd_dea_10_20_7, macd_hist_10_20_7
               FROM market_data.daily_kline
               WHERE symbol = %s AND trade_date >= %s AND trade_date <= %s
               ORDER BY trade_date""",
            (symbol, start_str, end_str),
        )
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=columns)
    df["date"] = pd.to_datetime(df["date"])
    # psycopg2 将 NUMERIC 列返回为 decimal.Decimal，pandas 3.x 不再自动转 float，
    # 统一转数值避免下游指标计算时 Decimal 与 float 混合运算报错
    for c in df.columns:
        if c != "date":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def save_kline(df: pd.DataFrame, symbol: str, config: DatabaseConfig | None = None) -> int:
    """批量写入日K线数据，已存在的 (symbol, trade_date) 会被更新（upsert）

    Args:
        df: 含 date/open/high/low/close/volume/amount/turnover 列的 DataFrame
        symbol: 股票代码

    Returns:
        写入的记录数
    """
    if df.empty:
        return 0

    def _val(row, key, cast=float):
        v = row.get(key)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        try:
            v = cast(v)
        except (ValueError, TypeError):
            return None
        # PostgreSQL numeric 不支持 inf/nan（除零等产生的非有限值），统一转 NULL
        if isinstance(v, float) and not math.isfinite(v):
            return None
        return v

    rows = []
    for _, row in df.iterrows():
        d = row["date"]
        date_str = d.strftime("%Y-%m-%d") if not isinstance(d, str) else d
        rows.append((
            symbol,
            date_str,
            _val(row, "open"),
            _val(row, "high"),
            _val(row, "low"),
            _val(row, "close"),
            _val(row, "volume", cast=int),
            _val(row, "amount"),
            _val(row, "turnover"),
            _val(row, "pct_change"),
            _val(row, "change"),
            _val(row, "amplitude"),
            _val(row, "volume_ratio"),
            _val(row, "main_net_inflow"),
            _val(row, "super_large_net_inflow"),
            _val(row, "large_net_inflow"),
            _val(row, "medium_net_inflow"),
            _val(row, "small_net_inflow"),
            _val(row, "macd_dif"),
            _val(row, "macd_dea"),
            _val(row, "macd_hist"),
            _val(row, "ma5"),
            _val(row, "ma10"),
            _val(row, "ma20"),
            _val(row, "ma30"),
            _val(row, "ma60"),
            _val(row, "ma120"),
            _val(row, "rsi14"),
            _val(row, "kdj_k"),
            _val(row, "kdj_d"),
            _val(row, "kdj_j"),
            _val(row, "bias5"),
            _val(row, "bias10"),
            _val(row, "bias20"),
            _val(row, "macd_dif_6_12_5"),
            _val(row, "macd_dea_6_12_5"),
            _val(row, "macd_hist_6_12_5"),
            _val(row, "macd_dif_3_8_3"),
            _val(row, "macd_dea_3_8_3"),
            _val(row, "macd_hist_3_8_3"),
            _val(row, "macd_dif_10_20_7"),
            _val(row, "macd_dea_10_20_7"),
            _val(row, "macd_hist_10_20_7"),
        ))

    with db_cursor(config) as cur:
        execute_values(
            cur,
            """INSERT INTO market_data.daily_kline
               (symbol, trade_date, open, high, low, close, volume, amount, turnover,
                pct_change, change, amplitude, volume_ratio,
                main_net_inflow, super_large_net_inflow, large_net_inflow,
                medium_net_inflow, small_net_inflow,
                macd_dif, macd_dea, macd_hist,
                ma5, ma10, ma20, ma30, ma60, ma120,
                rsi14, kdj_k, kdj_d, kdj_j,
                bias5, bias10, bias20,
                macd_dif_6_12_5, macd_dea_6_12_5, macd_hist_6_12_5,
                macd_dif_3_8_3, macd_dea_3_8_3, macd_hist_3_8_3,
                macd_dif_10_20_7, macd_dea_10_20_7, macd_hist_10_20_7)
               VALUES %s
               ON CONFLICT (symbol, trade_date) DO UPDATE SET
                   open = EXCLUDED.open,
                   high = EXCLUDED.high,
                   low = EXCLUDED.low,
                   close = EXCLUDED.close,
                   volume = EXCLUDED.volume,
                   amount = EXCLUDED.amount,
                   -- 腾讯源不提供换手率(恒为 NULL)，避免覆盖已回填的真实值
                   turnover = COALESCE(EXCLUDED.turnover, market_data.daily_kline.turnover),
                   pct_change = EXCLUDED.pct_change,
                   change = EXCLUDED.change,
                   amplitude = EXCLUDED.amplitude,
                   volume_ratio = EXCLUDED.volume_ratio,
                   main_net_inflow = EXCLUDED.main_net_inflow,
                   super_large_net_inflow = EXCLUDED.super_large_net_inflow,
                   large_net_inflow = EXCLUDED.large_net_inflow,
                   medium_net_inflow = EXCLUDED.medium_net_inflow,
                   small_net_inflow = EXCLUDED.small_net_inflow,
                   macd_dif = EXCLUDED.macd_dif,
                   macd_dea = EXCLUDED.macd_dea,
                   macd_hist = EXCLUDED.macd_hist,
                   ma5 = EXCLUDED.ma5,
                   ma10 = EXCLUDED.ma10,
                   ma20 = EXCLUDED.ma20,
                   ma30 = EXCLUDED.ma30,
                   ma60 = EXCLUDED.ma60,
                   ma120 = EXCLUDED.ma120,
                   rsi14 = EXCLUDED.rsi14,
                   kdj_k = EXCLUDED.kdj_k,
                   kdj_d = EXCLUDED.kdj_d,
                   kdj_j = EXCLUDED.kdj_j,
                   bias5 = EXCLUDED.bias5,
                   bias10 = EXCLUDED.bias10,
                   bias20 = EXCLUDED.bias20,
                   macd_dif_6_12_5 = EXCLUDED.macd_dif_6_12_5,
                   macd_dea_6_12_5 = EXCLUDED.macd_dea_6_12_5,
                   macd_hist_6_12_5 = EXCLUDED.macd_hist_6_12_5,
                   macd_dif_3_8_3 = EXCLUDED.macd_dif_3_8_3,
                   macd_dea_3_8_3 = EXCLUDED.macd_dea_3_8_3,
                   macd_hist_3_8_3 = EXCLUDED.macd_hist_3_8_3,
                   macd_dif_10_20_7 = EXCLUDED.macd_dif_10_20_7,
                   macd_dea_10_20_7 = EXCLUDED.macd_dea_10_20_7,
                   macd_hist_10_20_7 = EXCLUDED.macd_hist_10_20_7""",
            rows,
        )
    logger.info(f"日K线已入库: {symbol} {len(rows)} 条")
    return len(rows)
