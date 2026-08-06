"""数据库 CRUD 模块 - 策略、回测结果、交易记录、净值的读写"""

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
