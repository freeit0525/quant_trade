"""初始化 quant_trading 数据库表结构

- 创建模式: market_data, backtest, portfolio, strategy
- 创建表: daily_kline, stock_info, results, trades, nav, positions, config
"""
import sys
from pathlib import Path

# 确保项目根目录在 path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db import get_connection, db_cursor


def main():
    with db_cursor() as cur:
        # 创建模式
        schemas = ["market_data", "backtest", "portfolio", "strategy"]
        for schema in schemas:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            print(f"模式 {schema} 创建成功")

        # market_data.daily_kline - 日K线行情表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_data.daily_kline (
                id SERIAL PRIMARY KEY,
                symbol VARCHAR(20) NOT NULL,
                trade_date DATE NOT NULL,
                open NUMERIC(12,4),
                high NUMERIC(12,4),
                low NUMERIC(12,4),
                close NUMERIC(12,4),
                volume BIGINT,
                amount NUMERIC(20,4),
                turnover NUMERIC(12,4),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(symbol, trade_date)
            )
        """)
        print("表 market_data.daily_kline 创建成功")

        # market_data.stock_info - 股票基本信息表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_data.stock_info (
                symbol VARCHAR(10) PRIMARY KEY,
                name VARCHAR(20) NOT NULL,
                market VARCHAR(10),
                industry VARCHAR(50),
                concept TEXT[],
                list_date DATE,
                total_share NUMERIC(20,2),
                float_share NUMERIC(20,2),
                total_market_cap NUMERIC(20,2),
                float_market_cap NUMERIC(20,2),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 兼容旧表结构：补充缺失列
        for col, col_def in [("market", "VARCHAR(10)"), ("concept", "TEXT[]"),
                              ("total_share", "NUMERIC(20,2)"), ("float_share", "NUMERIC(20,2)"),
                              ("total_market_cap", "NUMERIC(20,2)"), ("float_market_cap", "NUMERIC(20,2)")]:
            try:
                cur.execute(f"ALTER TABLE market_data.stock_info ADD COLUMN IF NOT EXISTS {col} {col_def}")
            except Exception:
                pass
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_info_market ON market_data.stock_info(market)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_info_industry ON market_data.stock_info(industry)")
        print("表 market_data.stock_info 就绪")

        # backtest.results - 回测结果表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS backtest.results (
                id SERIAL PRIMARY KEY,
                strategy_name VARCHAR(100) NOT NULL,
                symbol VARCHAR(20) NOT NULL,
                start_date DATE NOT NULL,
                end_date DATE NOT NULL,
                initial_capital NUMERIC(20,4),
                final_capital NUMERIC(20,4),
                total_return NUMERIC(12,6),
                annual_return NUMERIC(12,6),
                max_drawdown NUMERIC(12,6),
                sharpe_ratio NUMERIC(12,6),
                params JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("表 backtest.results 创建成功")

        # backtest.trades - 回测交易记录表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS backtest.trades (
                id SERIAL PRIMARY KEY,
                result_id INTEGER REFERENCES backtest.results(id) ON DELETE CASCADE,
                symbol VARCHAR(20) NOT NULL,
                trade_type VARCHAR(10) NOT NULL,
                trade_date DATE NOT NULL,
                price NUMERIC(12,4) NOT NULL,
                quantity INTEGER NOT NULL,
                commission NUMERIC(12,4) DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("表 backtest.trades 创建成功")

        # backtest.nav - 回测每日净值表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS backtest.nav (
                id SERIAL PRIMARY KEY,
                result_id INTEGER REFERENCES backtest.results(id) ON DELETE CASCADE,
                nav_date DATE NOT NULL,
                cash NUMERIC(20,4),
                market_value NUMERIC(20,4),
                total_value NUMERIC(20,4),
                nav NUMERIC(12,6),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("表 backtest.nav 创建成功")

        # portfolio.positions - 持仓表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS portfolio.positions (
                id SERIAL PRIMARY KEY,
                symbol VARCHAR(20) NOT NULL,
                quantity INTEGER NOT NULL,
                avg_cost NUMERIC(12,4) NOT NULL,
                current_price NUMERIC(12,4),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("表 portfolio.positions 创建成功")

        # strategy.config - 策略配置表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS strategy.config (
                id SERIAL PRIMARY KEY,
                strategy_name VARCHAR(100) NOT NULL,
                strategy_type VARCHAR(50) NOT NULL,
                params JSONB NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("表 strategy.config 创建成功")

    print("\n所有数据库表结构初始化完成！")


if __name__ == "__main__":
    main()
