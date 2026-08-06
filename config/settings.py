"""配置模块 - 使用dataclass定义所有配置项"""

import os
from dataclasses import dataclass, field
from typing import Literal

from dotenv import load_dotenv

# 加载 .env 文件中的环境变量（数据库凭证等敏感信息必须通过环境变量注入，禁止硬编码）
load_dotenv()


@dataclass
class DatabaseConfig:
    """PostgreSQL 数据库配置

    凭证从 .env 读取；password 为必填项，缺失时 url 拼接为空字符串以便早期暴露连接错误。
    """
    host: str = os.getenv("DB_HOST", "localhost")
    port: int = int(os.getenv("DB_PORT", "5432"))
    dbname: str = os.getenv("DB_NAME", "quant_trading")
    user: str = os.getenv("DB_USER", "postgres")
    password: str = os.getenv("DB_PASSWORD", "")

    @property
    def url(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.dbname}"


@dataclass
class DataSourceConfig:
    """数据源配置"""
    # 数据源类型：akshare / eastmoney(直连东财) / tushare
    source: Literal["akshare", "eastmoney", "tushare"] = "akshare"
    # tushare token（使用tushare时需要）
    tushare_token: str = ""
    # 数据缓存目录
    cache_dir: str = "cache"


@dataclass
class BacktestConfig:
    """回测参数配置"""
    # 初始资金
    initial_capital: float = 1_000_000.0
    # 手续费率（买卖双边）
    commission_rate: float = 0.0003
    # 印花税率（卖出时收取）
    stamp_tax_rate: float = 0.001
    # 滑点（元/股）
    slippage: float = 0.01
    # 买卖最小单位（股）
    trade_unit: int = 100
    # 成交价格模式: "close"=当日收盘价成交, "next_open"=次日开盘价成交（更真实）
    fill_price_mode: str = "next_open"


@dataclass
class RiskConfig:
    """风险管理配置"""
    # 单只股票最大持仓比例（0~1）
    max_position_ratio: float = 0.3
    # 止损比例（0~1）
    stop_loss_ratio: float = 0.05
    # 止盈比例（0~1）
    take_profit_ratio: float = 0.15
    # 最大回撤预警阈值（0~1）
    max_drawdown_warning: float = 0.2


@dataclass
class LogConfig:
    """日志配置"""
    # 日志级别
    level: str = "INFO"
    # 日志格式
    format: str = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    # 日志输出文件（空字符串表示不输出到文件）
    file: str = ""


@dataclass
class Settings:
    """全局配置集合"""
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    data_source: DataSourceConfig = field(default_factory=DataSourceConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    log: LogConfig = field(default_factory=LogConfig)
