"""初始化 quant_trading 数据库表结构

- 创建模式: market_data, backtest, portfolio, strategy
- 创建表: daily_kline, stock_info, results, trades, nav, positions, config
"""
import sys
from pathlib import Path

# 确保项目根目录在 path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db import get_connection, db_cursor


# 字段注释：COMMENT ON COLUMN 为幂等操作，可用于已存在的表补注释
TABLE_COMMENTS = {
    "market_data.daily_kline": "日K线行情表",
    "market_data.stock_info": "股票基本信息表",
    "market_data.concept": "概念板块表",
    "market_data.stock_concept": "股票-概念关联表",
    "backtest.results": "回测结果表",
    "backtest.trades": "回测交易记录表",
    "backtest.nav": "回测每日净值表",
    "portfolio.positions": "持仓表",
    "strategy.config": "策略配置表",
}

COLUMN_COMMENTS = {
    "market_data.daily_kline": {
        "id": "主键",
        "symbol": "股票代码（如 600000）",
        "trade_date": "交易日期",
        "open": "开盘价（元）",
        "high": "最高价（元）",
        "low": "最低价（元）",
        "close": "收盘价（元）",
        "volume": "成交量（股）",
        "amount": "成交额（元）",
        "turnover": "换手率（%）",
        "pct_change": "涨跌幅（%）",
        "change": "涨跌额（元）",
        "amplitude": "振幅（%）",
        "volume_ratio": "量比（当日成交量 / 过去5日平均成交量）",
        "main_net_inflow": "主力净流入（元，超大单+大单）",
        "super_large_net_inflow": "超大单净流入（元）",
        "large_net_inflow": "大单净流入（元）",
        "medium_net_inflow": "中单净流入（元）",
        "small_net_inflow": "小单净流入（元）",
        "created_at": "记录创建时间",
    },
    "market_data.stock_info": {
        "symbol": "股票代码（主键）",
        "name": "股票名称",
        "market": "市场（sh/sz/bj）",
        "industry": "所属行业",
        "list_date": "上市日期",
        "total_share": "总股本（股）",
        "float_share": "流通股本（股）",
        "total_market_cap": "总市值（元）",
        "float_market_cap": "流通市值（元）",
        "created_at": "记录创建时间",
        "updated_at": "记录更新时间",
    },
    "market_data.concept": {
        "id": "主键",
        "code": "概念板块代码（东财 BKxxxx）",
        "name": "概念板块名称（如 抖音概念）",
        "created_at": "记录创建时间",
        "updated_at": "记录更新时间",
    },
    "market_data.stock_concept": {
        "symbol": "股票代码",
        "concept_id": "概念板块ID（关联 market_data.concept.id）",
    },
    "backtest.results": {
        "id": "主键",
        "strategy_name": "策略名称",
        "symbol": "回测股票代码",
        "start_date": "回测开始日期",
        "end_date": "回测结束日期",
        "initial_capital": "初始资金（元）",
        "final_capital": "期末资金（元）",
        "total_return": "总收益率",
        "annual_return": "年化收益率",
        "max_drawdown": "最大回撤",
        "sharpe_ratio": "夏普比率",
        "params": "策略参数（JSON）",
        "created_at": "记录创建时间",
    },
    "backtest.trades": {
        "id": "主键",
        "result_id": "回测结果ID（外键）",
        "symbol": "股票代码",
        "trade_type": "交易方向（buy买入/sell卖出）",
        "trade_date": "交易日期",
        "price": "成交价格（元）",
        "quantity": "成交数量（股）",
        "commission": "手续费（元）",
        "created_at": "记录创建时间",
    },
    "backtest.nav": {
        "id": "主键",
        "result_id": "回测结果ID（外键）",
        "nav_date": "净值日期",
        "cash": "现金余额（元）",
        "market_value": "持仓市值（元）",
        "total_value": "总资产（元，现金+市值）",
        "nav": "当日净值",
        "created_at": "记录创建时间",
    },
    "portfolio.positions": {
        "id": "主键",
        "symbol": "股票代码",
        "quantity": "持仓数量（股）",
        "avg_cost": "平均成本（元）",
        "current_price": "最新价（元）",
        "created_at": "记录创建时间",
        "updated_at": "记录更新时间",
    },
    "strategy.config": {
        "id": "主键",
        "strategy_name": "策略名称",
        "strategy_type": "策略类型（如 macd/均线）",
        "params": "策略参数（JSON）",
        "is_active": "是否启用",
        "created_at": "记录创建时间",
        "updated_at": "记录更新时间",
    },
}


def add_column_comments():
    """为所有表的字段补充注释（幂等）"""
    with db_cursor() as cur:
        for table, comment in TABLE_COMMENTS.items():
            cur.execute(f"COMMENT ON TABLE {table} IS %s", (comment,))
        for table, cols in COLUMN_COMMENTS.items():
            for col, comment in cols.items():
                cur.execute(f"COMMENT ON COLUMN {table}.{col} IS %s", (comment,))
    print("字段注释已补充完成")


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
                pct_change NUMERIC(12,4),
                change NUMERIC(12,4),
                amplitude NUMERIC(12,4),
                volume_ratio NUMERIC(12,4),
                main_net_inflow NUMERIC(20,4),
                super_large_net_inflow NUMERIC(20,4),
                large_net_inflow NUMERIC(20,4),
                medium_net_inflow NUMERIC(20,4),
                small_net_inflow NUMERIC(20,4),
                macd_dif NUMERIC(12,4),
                macd_dea NUMERIC(12,4),
                macd_hist NUMERIC(12,4),
                ma5 NUMERIC(12,4),
                ma10 NUMERIC(12,4),
                ma20 NUMERIC(12,4),
                ma30 NUMERIC(12,4),
                ma60 NUMERIC(12,4),
                ma120 NUMERIC(12,4),
                rsi14 NUMERIC(12,4),
                kdj_k NUMERIC(12,4),
                kdj_d NUMERIC(12,4),
                kdj_j NUMERIC(12,4),
                bias5 NUMERIC(12,4),
                bias10 NUMERIC(12,4),
                bias20 NUMERIC(12,4),
                macd_dif_6_12_5 NUMERIC(12,4),
                macd_dea_6_12_5 NUMERIC(12,4),
                macd_hist_6_12_5 NUMERIC(12,4),
                macd_dif_3_8_3 NUMERIC(12,4),
                macd_dea_3_8_3 NUMERIC(12,4),
                macd_hist_3_8_3 NUMERIC(12,4),
                macd_dif_10_20_7 NUMERIC(12,4),
                macd_dea_10_20_7 NUMERIC(12,4),
                macd_hist_10_20_7 NUMERIC(12,4),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(symbol, trade_date)
            )
        """)
        # 兼容旧表结构：补充新增列（涨跌幅/涨跌额/振幅/量比/资金流向/MACD/MA/RSI/KDJ）
        for col, col_def in [("pct_change", "NUMERIC(12,4)"),
                              ("change", "NUMERIC(12,4)"),
                              ("amplitude", "NUMERIC(12,4)"),
                              ("volume_ratio", "NUMERIC(12,4)"),
                              ("main_net_inflow", "NUMERIC(20,4)"),
                              ("super_large_net_inflow", "NUMERIC(20,4)"),
                              ("large_net_inflow", "NUMERIC(20,4)"),
                              ("medium_net_inflow", "NUMERIC(20,4)"),
                              ("small_net_inflow", "NUMERIC(20,4)"),
                              ("macd_dif", "NUMERIC(12,4)"),
                              ("macd_dea", "NUMERIC(12,4)"),
                              ("macd_hist", "NUMERIC(12,4)"),
                              ("ma5", "NUMERIC(12,4)"),
                              ("ma10", "NUMERIC(12,4)"),
                              ("ma20", "NUMERIC(12,4)"),
                              ("ma30", "NUMERIC(12,4)"),
                              ("ma60", "NUMERIC(12,4)"),
                              ("ma120", "NUMERIC(12,4)"),
                              ("rsi14", "NUMERIC(12,4)"),
                              ("kdj_k", "NUMERIC(12,4)"),
                              ("kdj_d", "NUMERIC(12,4)"),
                              ("kdj_j", "NUMERIC(12,4)"),
                              ("bias5", "NUMERIC(12,4)"),
                              ("bias10", "NUMERIC(12,4)"),
                              ("bias20", "NUMERIC(12,4)"),
                              ("macd_dif_6_12_5", "NUMERIC(12,4)"),
                              ("macd_dea_6_12_5", "NUMERIC(12,4)"),
                              ("macd_hist_6_12_5", "NUMERIC(12,4)"),
                              ("macd_dif_3_8_3", "NUMERIC(12,4)"),
                              ("macd_dea_3_8_3", "NUMERIC(12,4)"),
                              ("macd_hist_3_8_3", "NUMERIC(12,4)"),
                              ("macd_dif_10_20_7", "NUMERIC(12,4)"),
                              ("macd_dea_10_20_7", "NUMERIC(12,4)"),
                              ("macd_hist_10_20_7", "NUMERIC(12,4)")]:
            try:
                cur.execute(f"ALTER TABLE market_data.daily_kline ADD COLUMN IF NOT EXISTS {col} {col_def}")
            except Exception:
                pass
        print("表 market_data.daily_kline 创建成功")

        # market_data.stock_info - 股票基本信息表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_data.stock_info (
                symbol VARCHAR(10) PRIMARY KEY,
                name VARCHAR(20) NOT NULL,
                market VARCHAR(10),
                industry VARCHAR(50),
                list_date DATE,
                total_share NUMERIC(20,2),
                float_share NUMERIC(20,2),
                total_market_cap NUMERIC(20,2),
                float_market_cap NUMERIC(20,2),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 兼容旧表结构：补充缺失列；概念已拆分到 concept/stock_concept 表，移除旧列
        for col, col_def in [("market", "VARCHAR(10)"),
                              ("total_share", "NUMERIC(20,2)"), ("float_share", "NUMERIC(20,2)"),
                              ("total_market_cap", "NUMERIC(20,2)"), ("float_market_cap", "NUMERIC(20,2)")]:
            try:
                cur.execute(f"ALTER TABLE market_data.stock_info ADD COLUMN IF NOT EXISTS {col} {col_def}")
            except Exception:
                pass
        cur.execute("ALTER TABLE market_data.stock_info DROP COLUMN IF EXISTS concept")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_info_market ON market_data.stock_info(market)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_info_industry ON market_data.stock_info(industry)")
        print("表 market_data.stock_info 就绪")

        # market_data.concept - 概念板块表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_data.concept (
                id SERIAL PRIMARY KEY,
                code VARCHAR(20) UNIQUE NOT NULL,
                name VARCHAR(100) UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("表 market_data.concept 就绪")

        # market_data.stock_concept - 股票-概念关联表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_data.stock_concept (
                symbol VARCHAR(10) NOT NULL,
                concept_id INTEGER NOT NULL REFERENCES market_data.concept(id) ON DELETE CASCADE,
                PRIMARY KEY (symbol, concept_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_concept_concept ON market_data.stock_concept(concept_id)")
        print("表 market_data.stock_concept 就绪")

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

    # 补充表和字段注释（幂等）
    add_column_comments()

    print("\n所有数据库表结构初始化完成！")


if __name__ == "__main__":
    main()
