"""数据库 CRUD 模块 - 策略、回测结果、交易记录、净值的读写"""

import math
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

import pandas as pd
import psycopg2
import psycopg2.pool
from psycopg2.extras import execute_values

from config.settings import DatabaseConfig
from utils.logger import get_logger

logger = get_logger(__name__)

# 默认配置的连接池（远程库建连开销大，复用连接可显著提速）
_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_pool_lock = threading.Lock()


def _get_pool(config: DatabaseConfig | None) -> Optional[psycopg2.pool.ThreadedConnectionPool]:
    """获取默认配置的连接池；传入自定义 config 时返回 None（走直连）

    线程安全：ThreadedConnectionPool 内部有锁，适配 ThreadingHTTPServer 多线程。
    """
    global _pool
    if config is not None:
        return None
    with _pool_lock:
        if _pool is None:
            from config.settings import Settings
            cfg = Settings().database
            try:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    1, 8,
                    host=cfg.host, port=cfg.port, dbname=cfg.dbname,
                    user=cfg.user, password=cfg.password,
                )
                logger.info("数据库连接池已创建 (min=1, max=8)")
            except Exception as e:
                logger.error("创建数据库连接池失败: %s", e)
                _pool = None
        return _pool


def get_connection(config: DatabaseConfig | None = None):
    """获取数据库连接（直连，供单次使用场景）"""
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
    """数据库游标上下文管理器，自动提交和关闭（默认配置走连接池）"""
    pool = _get_pool(config)
    if pool is not None:
        conn = None
        try:
            conn = pool.getconn()
            # 池中连接可能已被服务端断开，失效则丢弃重取
            if conn.closed:
                try:
                    pool.putconn(conn, close=True)
                except Exception:
                    pass
                conn = pool.getconn()
            # 心跳探测：psycopg2 不主动感知服务端已断开的连接（conn.closed 仍为 False），
            # 直接复用会导致 execute 时报 "connection already closed"。用 SELECT 1 先探活，失效则丢弃重取。
            try:
                probe = conn.cursor()
                probe.execute("SELECT 1")
                probe.close()
            except (psycopg2.InterfaceError, psycopg2.OperationalError):
                try:
                    pool.putconn(conn, close=True)
                except Exception:
                    pass
                conn = pool.getconn()
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
            if conn is not None:
                pool.putconn(conn)
        return

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
    description: str | None = None,
    config: DatabaseConfig | None = None,
) -> int:
    """保存策略配置，返回策略ID"""
    import json
    with db_cursor(config) as cur:
        cur.execute(
            """INSERT INTO backtest.strategy (strategy_name, strategy_type, params, description, is_active)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (strategy_name, strategy_type, json.dumps(params, ensure_ascii=False), description, is_active),
        )
        strategy_id = cur.fetchone()[0]
    logger.info(f"策略已保存: {strategy_name}, id={strategy_id}")
    return strategy_id


def update_strategy(
    strategy_id: int,
    strategy_name: str | None = None,
    params: dict | None = None,
    is_active: bool | None = None,
    description: str | None = None,
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
    if description is not None:
        updates.append("description = %s")
        values.append(description)
    updates.append("updated_at = CURRENT_TIMESTAMP")
    values.append(strategy_id)

    with db_cursor(config) as cur:
        cur.execute(
            f"UPDATE backtest.strategy SET {', '.join(updates)} WHERE id = %s",
            values,
        )
    logger.info(f"策略已更新: id={strategy_id}")


def get_strategies(
    active_only: bool = False,
    config: DatabaseConfig | None = None,
) -> list[dict]:
    """获取所有策略配置"""
    with db_cursor(config) as cur:
        sql = "SELECT id, strategy_name, strategy_type, params, description, is_active, created_at, updated_at FROM backtest.strategy"
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
            "SELECT id, strategy_name, strategy_type, params, description, is_active, created_at, updated_at FROM backtest.strategy WHERE id = %s",
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
        cur.execute("DELETE FROM backtest.strategy WHERE id = %s", (strategy_id,))
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
    strategy_id: int | None = None,
    config: DatabaseConfig | None = None,
) -> int:
    """保存回测结果，返回结果ID（strategy_id 关联 backtest.strategy）"""
    import json
    with db_cursor(config) as cur:
        cur.execute(
            """INSERT INTO backtest.results
               (strategy_id, strategy_name, symbol, start_date, end_date, initial_capital, final_capital,
                total_return, annual_return, max_drawdown, sharpe_ratio, win_rate,
                total_trades, trading_days, total_commission, params)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (
                strategy_id, strategy_name, symbol, start_date, end_date,
                initial_capital, final_capital,
                total_return, annual_return, max_drawdown, sharpe_ratio,
                win_rate, total_trades, trading_days, total_commission,
                json.dumps(params, ensure_ascii=False) if params else None,
            ),
        )
        result_id = cur.fetchone()[0]
    logger.info(f"回测结果已保存: {strategy_name} {symbol}, result_id={result_id}")
    return result_id


def get_backtest_results(
    limit: int = 50,
    strategy_id: int | None = None,
    config: DatabaseConfig | None = None,
) -> list[dict]:
    """获取回测结果列表（可按策略ID过滤）"""
    with db_cursor(config) as cur:
        sql = """SELECT id, strategy_id, strategy_name, symbol, start_date, end_date,
                        initial_capital, final_capital, total_return, annual_return,
                        max_drawdown, sharpe_ratio, win_rate, total_trades, trading_days,
                        total_commission, params, created_at
                 FROM backtest.results"""
        conditions = []
        values = []
        if strategy_id is not None:
            conditions.append("strategy_id = %s")
            values.append(strategy_id)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY created_at DESC LIMIT %s"
        values.append(limit)
        cur.execute(sql, values)
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
    return [dict(zip(columns, row)) for row in rows]


def get_backtest_result_by_id(result_id: int, config: DatabaseConfig | None = None) -> Optional[dict]:
    """根据ID获取回测结果"""
    with db_cursor(config) as cur:
        cur.execute(
            """SELECT id, strategy_id, strategy_name, symbol, start_date, end_date,
                      initial_capital, final_capital, total_return, annual_return,
                      max_drawdown, sharpe_ratio, win_rate, total_trades, trading_days,
                      total_commission, params, created_at
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


def get_kline_range_info(symbol: str, config: DatabaseConfig | None = None) -> dict:
    """一次查询返回库中该股票的 [max_date, min_date, list_date]

    合并多次独立查询，减少往返（配合连接池使用）。
    Returns:
        {"max_date": 'YYYY-MM-DD'|None, "min_date": ..., "list_date": ...}
    """
    with db_cursor(config) as cur:
        cur.execute(
            """SELECT
                   (SELECT MAX(trade_date) FROM market_data.daily_kline WHERE symbol = %s),
                   (SELECT MIN(trade_date) FROM market_data.daily_kline WHERE symbol = %s),
                   (SELECT list_date FROM market_data.stock_info WHERE symbol = %s)""",
            (symbol, symbol, symbol),
        )
        row = cur.fetchone()
    return {
        "max_date": row[0].strftime("%Y-%m-%d") if row and row[0] else None,
        "min_date": row[1].strftime("%Y-%m-%d") if row and row[1] else None,
        "list_date": row[2].strftime("%Y-%m-%d") if row and row[2] else None,
    }


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
                      rsi6, rsi12, rsi14, rsi24, kdj_k, kdj_d, kdj_j,
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

    # 各列精度上限：numeric(12,4) 最大 10^8，numeric(20,4) 最大 10^16。
    # 源返回的脏数据若超限会让整批入库失败（numeric field overflow），
    # 导致整个缺口没存进去、下次同步重复拉取同一段数据，因此做钳制兜底。
    _LIMIT_12_4 = 99999999.9999
    _LIMIT_20_4 = 1e16 - 0.0001
    _COLS_20_4 = {
        "amount", "main_net_inflow", "super_large_net_inflow",
        "large_net_inflow", "medium_net_inflow", "small_net_inflow",
    }

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
        # 整数列（volume, bigint）不受 numeric 精度限制；浮点列超限时钳制
        if isinstance(v, float):
            lim = _LIMIT_20_4 if key in _COLS_20_4 else _LIMIT_12_4
            if v > lim:
                logger.warning("值超出列 %s 精度上限，已钳制: %s", key, v)
                v = lim
            elif v < -lim:
                logger.warning("值超出列 %s 精度下限，已钳制: %s", key, v)
                v = -lim
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
            _val(row, "rsi6"),
            _val(row, "rsi12"),
            _val(row, "rsi14"),
            _val(row, "rsi24"),
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
                rsi6, rsi12, rsi14, rsi24, kdj_k, kdj_d, kdj_j,
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
                   rsi6 = EXCLUDED.rsi6,
                   rsi12 = EXCLUDED.rsi12,
                   rsi14 = EXCLUDED.rsi14,
                   rsi24 = EXCLUDED.rsi24,
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


# ============================================================
# 买卖点预测记录 CRUD（含次日自动复盘）
# ============================================================

# 方向代码 -> 统计用方向分组（持有/观望只记录涨跌不计命中）
# 2026-08-11 操作建议体系：加仓(add)/持有(hold)/观望(watch)/减仓(trim)/卖出(sell)
_PRED_BUY_CODES = ("add",)           # 加仓：次日上涨=命中
_PRED_SELL_CODES = ("trim", "sell")  # 减仓/卖出：次日下跌=命中


def classify_trend(prev: dict, cur: dict) -> str:
    """根据两日K线判定当日走势形态（前后端一致，供复盘使用）

    Args:
        prev: 前一交易日 {open, high, low, close, volume}
        cur:  当日 {open, high, low, close, volume}

    Returns:
        形态key: surge_up放量大涨 / gap_up_high高开高走 / open_high平开高走 /
                 dip_recover探底回升 / range日内震荡 / rise_fall冲高回落 /
                 gap_up_low高开低走 / open_low低开低走 / surge_down放量大跌
    """
    try:
        prev_close = float(prev["close"])
        o, h, l, c = (float(cur[k]) for k in ("open", "high", "low", "close"))
        prev_vol = float(prev.get("volume") or 0)
        cur_vol = float(cur.get("volume") or 0)
    except (KeyError, TypeError, ValueError):
        return "range"
    if prev_close <= 0 or h <= l:
        return "range"
    pct = (c / prev_close - 1) * 100
    body = c - o
    gap = (o / prev_close - 1) * 100          # 高开/低开幅度
    amplitude = h - l
    upper = h - max(o, c)                     # 上影线长度
    lower = min(o, c) - l                     # 下影线长度
    surge = (cur_vol > 0 and prev_vol > 0 and cur_vol >= prev_vol * 1.5) or amplitude > 0.06 * prev_close
    # 判定优先级：放量极端方向 > 高开方向 > 低开方向 > 平开影线/实体
    if pct >= 3 and surge:
        return "surge_up"
    if pct <= -3 and surge:
        return "surge_down"
    if gap >= 0.8:                            # 高开
        if c > o and pct >= 1.5:
            return "gap_up_high"              # 高开高走
        return "gap_up_low"                   # 高开低走（含高开冲高回落）
    if gap <= -0.8:                           # 低开
        if c > o and (lower >= 0.45 * amplitude or pct >= 1.5):
            return "dip_recover"              # 探底回升（低开反包）
        return "open_low"                     # 低开低走
    # 平开附近：先看影线结构，再看实体方向
    if c < o and upper >= 0.6 * amplitude:
        return "rise_fall"                    # 长上影收阴：冲高回落
    if c > o and lower >= 0.6 * amplitude:
        return "dip_recover"                  # 长下影收阳：探底回升
    if c > o and pct >= 1.5 and upper <= 0.4 * amplitude:
        return "open_high"                    # 平开高走
    if c < o and pct <= -1.5:
        return "open_low"                     # 平开低走
    return "range"                            # 日内震荡


# 形态中文名（与前端一致）
TREND_NAMES = {
    "surge_up": "放量大涨", "gap_up_high": "高开高走", "open_high": "平开高走",
    "dip_recover": "探底回升", "range": "日内震荡", "rise_fall": "冲高回落",
    "gap_up_low": "高开低走", "open_low": "低开低走", "surge_down": "放量大跌",
}


def save_prediction(
    symbol: str,
    based_date: str,
    predict_date: str,
    action: str,
    action_code: str,
    name: str | None = None,
    score: float | None = None,
    prob_up: float | None = None,
    close: float | None = None,
    support_price: float | None = None,
    support2_price: float | None = None,
    resistance_price: float | None = None,
    resistance2_price: float | None = None,
    stop_loss: float | None = None,
    factors: list | None = None,
    weights: dict | None = None,
    trend: str | None = None,
    trend_probs: dict | None = None,
    trend_strategy: str | None = None,
    config: DatabaseConfig | None = None,
) -> int:
    """保存/更新预测结论（同一股票同一基于日期只保留一条，重复保存覆盖）

    Args:
        symbol: 股票代码
        based_date: 预测依据的最新收盘日 'YYYY-MM-DD'
        predict_date: 预测目标日（下一交易日）'YYYY-MM-DD'
        action: 结论文案（加仓/持有/观望/减仓/卖出）
        action_code: 方向代码 add/hold/watch/trim/sell（持有hold/观望watch只记录涨跌不计命中）
        factors/weights: 因子得分与权重（JSON 序列化落库）

    Returns:
        预测记录ID
    """
    import json

    with db_cursor(config) as cur:
        cur.execute(
            """INSERT INTO market_data.predictions
               (symbol, name, based_date, predict_date, action, action_code, score, prob_up,
                close, support_price, support2_price, resistance_price, resistance2_price,
                stop_loss, factors, weights, trend, trend_probs, trend_strategy)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (symbol, based_date) DO UPDATE SET
                   name = EXCLUDED.name,
                   predict_date = EXCLUDED.predict_date,
                   action = EXCLUDED.action,
                   action_code = EXCLUDED.action_code,
                   score = EXCLUDED.score,
                   prob_up = EXCLUDED.prob_up,
                   close = EXCLUDED.close,
                   support_price = EXCLUDED.support_price,
                   support2_price = EXCLUDED.support2_price,
                   resistance_price = EXCLUDED.resistance_price,
                   resistance2_price = EXCLUDED.resistance2_price,
                   stop_loss = EXCLUDED.stop_loss,
                   factors = EXCLUDED.factors,
                   weights = EXCLUDED.weights,
                   trend = EXCLUDED.trend,
                   trend_probs = EXCLUDED.trend_probs,
                   trend_strategy = EXCLUDED.trend_strategy,
                   updated_at = CURRENT_TIMESTAMP
               RETURNING id""",
            (
                symbol, name, based_date, predict_date, action, action_code,
                float(score) if score is not None else None,
                float(prob_up) if prob_up is not None else None,
                float(close) if close is not None else None,
                float(support_price) if support_price is not None else None,
                float(support2_price) if support2_price is not None else None,
                float(resistance_price) if resistance_price is not None else None,
                float(resistance2_price) if resistance2_price is not None else None,
                float(stop_loss) if stop_loss is not None else None,
                json.dumps(factors, ensure_ascii=False) if factors is not None else None,
                json.dumps(weights, ensure_ascii=False) if weights is not None else None,
                trend,
                json.dumps(trend_probs, ensure_ascii=False) if trend_probs is not None else None,
                trend_strategy,
            ),
        )
        pred_id = cur.fetchone()[0]
    logger.info(f"预测结论已保存: {symbol} based={based_date} action={action}, id={pred_id}")
    return pred_id


def _review_predictions(config: DatabaseConfig | None = None) -> int:
    """对未复盘的预测记录自动复盘（幂等）

    用库中 K 线找到基于日期之后的首个实际交易日，比较其收盘涨跌与预测方向：
    - 加仓(add)：实际涨 > 0 记命中
    - 减仓/卖出(trim/sell)：实际涨 < 0 记命中
    - 持有/观望(hold/watch)：记录实际涨跌但不计命中(hit=null)

    Returns:
        本次新复盘完成的记录数
    """
    import bisect

    with db_cursor(config) as cur:
        cur.execute(
            "SELECT id, symbol, based_date, action_code, trend FROM market_data.predictions WHERE review_date IS NULL"
        )
        pending = cur.fetchall()
        if not pending:
            return 0

        # 每个涉及股票加载全量 (trade_date, open, high, low, close, volume) 排序列表，二分定位复盘日
        symbols = sorted({r[1] for r in pending})
        klines: dict[str, list] = {}
        for sym in symbols:
            cur.execute(
                "SELECT trade_date, open, high, low, close, volume FROM market_data.daily_kline WHERE symbol = %s ORDER BY trade_date",
                (sym,),
            )
            klines[sym] = cur.fetchall()

        reviewed = 0
        for pid, sym, based_date, action_code, trend in pending:
            kk = klines.get(sym)
            if not kk:
                continue
            dates = [d for d, *_ in kk]
            # 基于日期后的首个交易日（严格大于）
            ridx = bisect.bisect_right(dates, based_date)
            if ridx >= len(kk):
                continue  # 目标日数据尚未入库，等下次复盘
            # 基于日期当天K线（作为涨跌基准与形态 prev）
            bidx = bisect.bisect_left(dates, based_date)
            if bidx >= len(kk) or dates[bidx] != based_date:
                continue  # 基于日期不在库中，无法计算基准
            base_row, review_row = kk[bidx], kk[ridx]
            base_close = base_row[4]
            review_close = review_row[4]
            if base_close is None or review_close is None or base_close == 0:
                continue
            pct = float(review_close) / float(base_close) - 1
            if action_code in _PRED_BUY_CODES:
                hit = pct > 0
            elif action_code in _PRED_SELL_CODES:
                hit = pct < 0
            else:
                hit = None  # 观望：记录涨跌但不计命中
            # 走势形态复盘：用基于日与复盘日两日K线判定实际形态
            prev_d = {"open": base_row[1], "high": base_row[2], "low": base_row[3],
                      "close": base_row[4], "volume": base_row[5]}
            cur_d = {"open": review_row[1], "high": review_row[2], "low": review_row[3],
                     "close": review_row[4], "volume": review_row[5]}
            actual_trend = classify_trend(prev_d, cur_d)
            trend_hit = (trend == actual_trend) if trend and actual_trend else None
            cur.execute(
                """UPDATE market_data.predictions
                   SET review_date = %s, actual_close = %s, actual_pct = %s,
                       hit = %s, actual_trend = %s, trend_hit = %s,
                       reviewed_at = CURRENT_TIMESTAMP
                   WHERE id = %s""",
                (kk[ridx][0], review_close, round(pct * 100, 4), hit,
                 actual_trend, trend_hit, pid),
            )
            reviewed += 1
        if reviewed:
            logger.info(f"自动复盘完成 {reviewed} 条预测记录")
        return reviewed


_PRED_COLUMNS = (
    "id, symbol, name, based_date, predict_date, action, action_code, score, prob_up, close, "
    "support_price, support2_price, resistance_price, resistance2_price, stop_loss, factors, weights, "
    "trend, trend_probs, trend_strategy, actual_trend, trend_hit, "
    "review_date, actual_close, actual_pct, hit, created_at, updated_at"
)


def get_pending_prediction_symbols(config: DatabaseConfig | None = None) -> list[str]:
    """返回存在未复盘预测的股票代码列表（用于触发这些股票的行情增量同步后自动复盘）"""
    with db_cursor(config) as cur:
        cur.execute(
            "SELECT DISTINCT symbol FROM market_data.predictions WHERE review_date IS NULL"
        )
        return [r[0] for r in cur.fetchall()]


def get_predictions(
    symbol: str | None = None,
    limit: int = 100,
    config: DatabaseConfig | None = None,
) -> list[dict]:
    """获取预测记录列表（先自动复盘未复盘记录）

    Args:
        symbol: 股票代码过滤（可选）
        limit: 返回条数上限

    Returns:
        按基于日期倒序的预测记录列表，日期字段为 'YYYY-MM-DD'，JSON 字段已解析
    """
    import json

    _review_predictions(config)
    with db_cursor(config) as cur:
        sql = f"SELECT {_PRED_COLUMNS} FROM market_data.predictions"
        args: list = []
        if symbol:
            sql += " WHERE symbol = %s"
            args.append(symbol)
        sql += " ORDER BY based_date DESC, id DESC LIMIT %s"
        args.append(limit)
        cur.execute(sql, args)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()

    result = []
    for row in rows:
        d = dict(zip(columns, row))
        for key in ("factors", "weights", "trend_probs"):
            if isinstance(d.get(key), str):
                try:
                    d[key] = json.loads(d[key])
                except (ValueError, TypeError):
                    d[key] = None
        for key in ("based_date", "predict_date", "review_date"):
            if d.get(key):
                d[key] = d[key].strftime("%Y-%m-%d")
        for key in ("created_at", "updated_at", "reviewed_at"):
            if d.get(key):
                d[key] = d[key].strftime("%Y-%m-%d %H:%M")
        for key in ("score", "prob_up", "close", "support_price", "support2_price",
                    "resistance_price", "resistance2_price", "stop_loss",
                    "actual_close", "actual_pct"):
            if d.get(key) is not None:
                d[key] = float(d[key])
        result.append(d)

    # 后5日涨跌（辅助口径，次日复盘为主口径）：基于日之后第5个交易日收盘 vs 基于日收盘
    if result:
        import bisect
        syms = sorted({d["symbol"] for d in result if d.get("symbol")})
        close_map: dict[str, list] = {}
        if syms:
            with db_cursor(config) as cur:
                for sym in syms:
                    cur.execute(
                        "SELECT trade_date, close FROM market_data.daily_kline WHERE symbol = %s ORDER BY trade_date",
                        (sym,),
                    )
                    close_map[sym] = cur.fetchall()
        for d in result:
            rows_c = close_map.get(d.get("symbol"))
            based = d.get("based_date")
            base_close = d.get("close")
            if not rows_c or not based or not base_close:
                continue
            dates = [r[0].strftime("%Y-%m-%d") for r in rows_c]
            ridx = bisect.bisect_right(dates, based)
            ridx5 = ridx + 4  # ridx 为次日，+4 为第5个交易日
            if ridx5 < len(rows_c):
                c5 = rows_c[ridx5][1]
                if c5 is not None and base_close:
                    d["ret5"] = round(float(c5) / float(base_close) - 1, 6)
                    d["ret5_date"] = dates[ridx5]
    return result


def get_prediction_stats(config: DatabaseConfig | None = None) -> dict:
    """预测记录汇总统计（供迭代总结/生成量化策略建议）

    先自动复盘，再统计：
    - 总体/按方向/按股票的命中率
    - 因子有效性：命中与未命中记录中各因子平均得分的差异（正=因子方向正确，负=需反向）
    - 基于样本量给出量化策略建议文案
    """
    import json

    _review_predictions(config)

    def _rate(hits, reviewed):
        return round(hits / reviewed * 100, 1) if reviewed else None

    with db_cursor(config) as cur:
        cur.execute(
            """SELECT COUNT(*),
                      COUNT(review_date),
                      COUNT(*) FILTER (WHERE hit = TRUE),
                      COUNT(*) FILTER (WHERE hit = FALSE),
                      COUNT(trend),
                      COUNT(trend_hit),
                      COUNT(*) FILTER (WHERE trend_hit = TRUE)
               FROM market_data.predictions"""
        )
        total, reviewed, hits, misses, trend_total, trend_reviewed, trend_hits = cur.fetchone()

        cur.execute(
            """SELECT action, action_code, COUNT(*),
                      COUNT(review_date),
                      COUNT(*) FILTER (WHERE hit = TRUE),
                      COUNT(*) FILTER (WHERE hit = FALSE),
                      ROUND(AVG(actual_pct) FILTER (WHERE review_date IS NOT NULL)::numeric, 4)
               FROM market_data.predictions
               GROUP BY action, action_code ORDER BY COUNT(*) DESC"""
        )
        by_action = [
            {
                "action": r[0], "action_code": r[1], "total": r[2],
                "reviewed": r[3], "hits": r[4], "misses": r[5],
                "hit_rate": _rate(r[4], r[3]),
                "avg_pct": float(r[6]) if r[6] is not None else None,
            }
            for r in cur.fetchall()
        ]

        cur.execute(
            """SELECT symbol, name, COUNT(*),
                      COUNT(review_date),
                      COUNT(*) FILTER (WHERE hit = TRUE),
                      COUNT(*) FILTER (WHERE hit = FALSE)
               FROM market_data.predictions
               GROUP BY symbol, name ORDER BY COUNT(*) DESC LIMIT 20"""
        )
        by_symbol = [
            {
                "symbol": r[0], "name": r[1], "total": r[2],
                "reviewed": r[3], "hits": r[4], "misses": r[5],
                "hit_rate": _rate(r[4], r[3]),
            }
            for r in cur.fetchall()
        ]

        # 命中/未命中记录的各因子得分，用于迭代总结因子有效性
        cur.execute(
            """SELECT hit, factors FROM market_data.predictions
               WHERE review_date IS NOT NULL AND hit IS NOT NULL AND factors IS NOT NULL"""
        )
        hit_rows = cur.fetchall()

    def _agg_group(hit_rows, target_hit):
        agg: dict[str, list[float]] = {}
        for hit, factors in hit_rows:
            if hit is not target_hit or not isinstance(factors, str):
                continue
            try:
                factors = json.loads(factors)
            except (ValueError, TypeError):
                continue
            for f in factors or []:
                if not isinstance(f, dict) or f.get("score") is None:
                    continue
                agg.setdefault(f.get("key") or f.get("name"), []).append(float(f["score"]))
        return {k: sum(v) / len(v) for k, v in agg.items()}

    factor_aligned, factor_inverted = [], []
    if hit_rows:
        hit_avg = _agg_group(hit_rows, True)
        miss_avg = _agg_group(hit_rows, False)
        deltas = []
        for key in set(hit_avg) | set(miss_avg):
            if key not in hit_avg or key not in miss_avg:
                continue
            deltas.append({"key": key, "hit_avg": round(hit_avg[key], 1),
                           "miss_avg": round(miss_avg[key], 1),
                           "delta": round(hit_avg[key] - miss_avg[key], 1)})
        deltas.sort(key=lambda d: abs(d["delta"]), reverse=True)
        factor_aligned = [d for d in deltas if d["delta"] > 0][:3]
        factor_inverted = [d for d in deltas if d["delta"] < 0][:3]

    # 买入/卖出方向汇总（供策略建议）
    buy = next((a for a in by_action if a["action_code"] in _PRED_BUY_CODES and a["reviewed"]), None)
    sell = next((a for a in by_action if a["action_code"] in _PRED_SELL_CODES and a["reviewed"]), None)
    buy_rate = _rate(buy["hits"], buy["reviewed"]) if buy else None
    sell_rate = _rate(sell["hits"], sell["reviewed"]) if sell else None

    # 量化策略建议（随样本量增长逐步收敛）
    trend_rate = _rate(trend_hits, trend_reviewed)
    suggestion = _build_strategy_suggestion(
        total=total, reviewed=reviewed, hit_rate=_rate(hits, reviewed),
        buy_rate=buy_rate, sell_rate=sell_rate,
        trend_rate=trend_rate, trend_reviewed=trend_reviewed,
        factor_aligned=factor_aligned, factor_inverted=factor_inverted,
    )

    return {
        "total": total, "reviewed": reviewed, "hits": hits, "misses": misses,
        "hit_rate": _rate(hits, reviewed),
        "trend": {"total": trend_total, "reviewed": trend_reviewed,
                  "hits": trend_hits, "hit_rate": trend_rate},
        "buy": {"hits": buy["hits"] if buy else 0, "reviewed": buy["reviewed"] if buy else 0,
                "hit_rate": buy_rate,
                "avg_pct": buy["avg_pct"] if buy else None},
        "sell": {"hits": sell["hits"] if sell else 0, "reviewed": sell["reviewed"] if sell else 0,
                 "hit_rate": sell_rate,
                 "avg_pct": sell["avg_pct"] if sell else None},
        "by_action": by_action,
        "by_symbol": by_symbol,
        "factor_aligned": factor_aligned,
        "factor_inverted": factor_inverted,
        "suggestion": suggestion,
    }


def _build_strategy_suggestion(
    total: int, reviewed: int, hit_rate: float | None,
    buy_rate: float | None, sell_rate: float | None,
    trend_rate: float | None, trend_reviewed: int,
    factor_aligned: list, factor_inverted: list,
) -> str:
    """根据复盘样本生成量化策略建议文案（样本越多建议越具体）"""
    if reviewed < 5:
        return (f"复盘样本不足（已复盘 {reviewed}/{total} 条，需≥5条）。"
                "继续每天保存预测结论，次日自动复盘，样本积累后自动给出量化策略。")
    parts = [f"已复盘 {reviewed} 条，总体命中率 {hit_rate}%"]
    if buy_rate is not None:
        parts.append(f"买入信号命中率 {buy_rate}%（方向正确率，非收益率）")
    if sell_rate is not None:
        parts.append(f"卖出信号命中率 {sell_rate}%")
    strategy = "可轻仓跟随买入信号" if buy_rate and buy_rate >= 55 else "买入信号暂不可靠，谨慎观望"
    strategy += "；" + ("卖出信号可信" if sell_rate and sell_rate >= 55 else "卖出信号参考性弱") + "。"
    if trend_rate is not None and trend_reviewed >= 5:
        strategy += (f"走势形态预测命中率 {trend_rate}%（{trend_reviewed}次），"
                     + ("可作为次日操盘预案参考。" if trend_rate >= 45 else "暂不可靠，仅作风格提示。"))
    if factor_aligned:
        good = "、".join(f["key"] for f in factor_aligned)
        strategy += f"表现好的因子：{good}，可上调权重。"
    if factor_inverted:
        bad = "、".join(f["key"] for f in factor_inverted)
        strategy += f"表现反的因子：{bad}，建议降权或反向理解。"
    return "，".join(parts) + "。" + strategy

