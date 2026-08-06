"""数据获取模块 - 支持通过akshare获取A股日线数据"""

import os
import re
from pathlib import Path
from typing import Optional

import pandas as pd

from config.settings import DataSourceConfig
from utils.logger import get_logger

logger = get_logger(__name__)


class DataFetcher:
    """A股数据获取器，支持akshare数据源和本地CSV缓存"""

    def __init__(self, config: DataSourceConfig | None = None) -> None:
        self.config = config or DataSourceConfig()
        self._cache_dir = Path(self.config.cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_cache_path(self, symbol: str, start_date: str, end_date: str) -> Path:
        """获取缓存文件路径"""
        filename = f"{symbol}_{start_date}_{end_date}.csv"
        return self._cache_dir / filename

    def _load_from_cache(self, symbol: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """从本地CSV缓存加载数据"""
        cache_path = self._get_cache_path(symbol, start_date, end_date)
        if cache_path.exists():
            logger.info(f"从缓存加载数据: {cache_path}")
            df = pd.read_csv(cache_path, parse_dates=["date"])
            return df
        return None

    def _save_to_cache(self, df: pd.DataFrame, symbol: str, start_date: str, end_date: str) -> None:
        """将数据保存到本地CSV缓存"""
        cache_path = self._get_cache_path(symbol, start_date, end_date)
        df.to_csv(cache_path, index=False, encoding="utf-8")
        logger.info(f"数据已缓存到: {cache_path}")

    def _normalize_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """统一列名为标准格式"""
        column_mapping = {
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
            "振幅": "amplitude",
            "涨跌幅": "pct_change",
            "涨跌额": "change",
            "换手率": "turnover",
        }
        df = df.rename(columns=column_mapping)
        # 确保date列为datetime类型
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
        return df

    def _find_wider_cache(self, symbol: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """查找能覆盖请求日期范围的缓存文件"""
        pattern = re.compile(rf"^{symbol}_(\d{{8}})_(\d{{8}})\.csv$")
        for f in self._cache_dir.iterdir():
            m = pattern.match(f.name)
            if m:
                cache_start, cache_end = m.group(1), m.group(2)
                if cache_start <= start_date and cache_end >= end_date:
                    logger.info(f"从宽范围缓存加载数据: {f.name}")
                    df = pd.read_csv(f, parse_dates=["date"])
                    df = self._normalize_columns(df)
                    start_dt = pd.to_datetime(start_date)
                    end_dt = pd.to_datetime(end_date)
                    df = df[(df["date"] >= start_dt) & (df["date"] <= end_dt)].reset_index(drop=True)
                    return df
        return None

    def fetch_stock(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        use_cache: bool = True,
        use_db: bool = True,
    ) -> pd.DataFrame:
        """
        获取单只股票的日线数据

        Args:
            symbol: 股票代码，如 "000001"
            start_date: 开始日期，格式 "YYYYMMDD"
            end_date: 结束日期，格式 "YYYYMMDD"
            use_cache: 是否使用本地CSV缓存（仅 use_db=False 时生效）
            use_db: 是否走数据库增量模式：先查库中已有最大日期，仅拉取缺失部分入库，
                    再从库中返回完整范围。重复执行只拉增量。

        Returns:
            包含 open/high/low/close/volume 等字段的 DataFrame
        """
        if use_db:
            return self._fetch_stock_via_db(symbol, start_date, end_date)

        # 尝试从缓存加载
        if use_cache:
            cached = self._load_from_cache(symbol, start_date, end_date)
            if cached is not None:
                return cached
            # 精确缓存未命中，尝试从宽范围缓存截取
            wider = self._find_wider_cache(symbol, start_date, end_date)
            if wider is not None:
                return wider

        if self.config.source == "akshare":
            df = self._fetch_via_akshare(symbol, start_date, end_date)
        elif self.config.source == "tushare":
            df = self._fetch_via_tushare(symbol, start_date, end_date)
        else:
            raise ValueError(f"不支持的数据源: {self.config.source}")

        if df.empty:
            logger.warning(f"未获取到数据: {symbol} ({start_date} ~ {end_date})")
            return df

        # 标准化列名
        df = self._normalize_columns(df)
        # 按日期升序排列
        df = df.sort_values("date").reset_index(drop=True)

        # 保存缓存
        if use_cache:
            self._save_to_cache(df, symbol, start_date, end_date)

        logger.info(f"成功获取 {symbol} 数据，共 {len(df)} 条记录")
        return df

    def _fetch_stock_via_db(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """数据库增量拉取：先查库中已有最大日期，仅拉取缺失部分入库，再从库返回完整范围"""
        from database.db import get_kline, get_kline_max_date, save_kline

        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        start_str = start_dt.strftime("%Y-%m-%d")
        end_str = end_dt.strftime("%Y-%m-%d")

        # 1. 对比数据库中是否已有数据
        max_date_str = get_kline_max_date(symbol)

        if max_date_str is not None:
            max_dt = pd.to_datetime(max_date_str)
            if max_dt >= end_dt:
                # 数据库已覆盖请求范围，直接返回库中数据
                logger.info(f"数据库已有 {symbol} 数据（至 {max_date_str}），覆盖请求范围，直接读取")
                return get_kline(symbol, start_str, end_str)
            # 增量拉取 max_date 次日 ~ end_date
            fetch_start = (max_dt + pd.Timedelta(days=1)).strftime("%Y%m%d")
            fetch_end = end_dt.strftime("%Y%m%d")
            logger.info(f"数据库已有 {symbol} 至 {max_date_str}，增量拉取 {fetch_start} ~ {fetch_end}")
        else:
            # 数据库无数据，全量拉取
            fetch_start = start_dt.strftime("%Y%m%d")
            fetch_end = end_dt.strftime("%Y%m%d")
            logger.info(f"数据库无 {symbol} 数据，全量拉取 {fetch_start} ~ {fetch_end}")

        # 2. 拉取缺失部分
        if self.config.source == "akshare":
            df = self._fetch_via_akshare(symbol, fetch_start, fetch_end)
        elif self.config.source == "tushare":
            df = self._fetch_via_tushare(symbol, fetch_start, fetch_end)
        else:
            raise ValueError(f"不支持的数据源: {self.config.source}")

        if df.empty:
            logger.warning(f"未获取到数据: {symbol} ({fetch_start} ~ {fetch_end})")
            return get_kline(symbol, start_str, end_str)

        df = self._normalize_columns(df)
        df = df.sort_values("date").reset_index(drop=True)

        # 3. 计算量比（需库中 fetch_start 之前最近5个交易日的 volume 作为窗口前置）
        df = self._calc_volume_ratio(df, symbol, fetch_start)

        # 4. 合并资金流向（近100天可获取，更早为 NULL）
        df = self._merge_fund_flow(df, symbol)

        # 5. 入库（upsert，已存在的日期会被更新）
        save_kline(df, symbol)

        # 6. 从数据库返回完整范围
        return get_kline(symbol, start_str, end_str)

    def _calc_volume_ratio(self, df: pd.DataFrame, symbol: str, fetch_start: str) -> pd.DataFrame:
        """计算量比 = 当日成交量 / 过去5个交易日（不含当日）平均成交量

        增量拉取时需拼接库中 fetch_start 之前最近5个交易日的 volume 作为滚动窗口前置，
        否则新拉取的前几行量比会因窗口不足而缺失。
        """
        if df.empty or "volume" not in df.columns:
            return df

        from database.db import get_kline

        # 取库中 fetch_start 之前约60个自然日内的数据，取末尾5个交易日
        fetch_start_dt = pd.to_datetime(fetch_start)
        before_start = (fetch_start_dt - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
        before_end = (fetch_start_dt - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        pre = get_kline(symbol, before_start, before_end)
        if not pre.empty:
            pre = pre.tail(5)[["date", "volume"]]
            combined = pd.concat([pre, df[["date", "volume"]]], ignore_index=True)
        else:
            combined = df[["date", "volume"]].copy()

        # 过去5日均量（不含当日）：rolling(5).mean() 后 shift(1)
        combined["volume_ratio"] = (
            combined["volume"] / combined["volume"].rolling(5).mean().shift(1)
        )
        ratio_map = dict(zip(combined["date"], combined["volume_ratio"]))
        df["volume_ratio"] = df["date"].map(ratio_map)
        return df

    def _infer_market_code(self, symbol: str) -> str:
        """根据股票代码推断交易所代码（sh/sz/bj），用于资金流向接口"""
        if symbol.startswith("6") or symbol.startswith("9"):
            return "sh"
        if symbol.startswith("0") or symbol.startswith("3"):
            return "sz"
        return "bj"

    def _fetch_fund_flow(self, symbol: str) -> pd.DataFrame:
        """通过 akshare 获取个股资金流向（近100个交易日），返回标准化DataFrame"""
        import akshare as ak

        market = self._infer_market_code(symbol)
        try:
            ff = ak.stock_individual_fund_flow(stock=symbol, market=market)
        except Exception as e:
            logger.error(f"资金流向获取失败 {symbol}: {e}")
            return pd.DataFrame()

        if ff is None or ff.empty:
            return pd.DataFrame()

        col_map = {
            "日期": "date",
            "主力净流入-净额": "main_net_inflow",
            "超大单净流入-净额": "super_large_net_inflow",
            "大单净流入-净额": "large_net_inflow",
            "中单净流入-净额": "medium_net_inflow",
            "小单净流入-净额": "small_net_inflow",
        }
        ff = ff.rename(columns=col_map)
        if "date" not in ff.columns:
            logger.warning(f"资金流向返回无日期列 {symbol}")
            return pd.DataFrame()
        ff["date"] = pd.to_datetime(ff["date"])
        keep = ["date", "main_net_inflow", "super_large_net_inflow",
                "large_net_inflow", "medium_net_inflow", "small_net_inflow"]
        return ff[[c for c in keep if c in ff.columns]]

    def _merge_fund_flow(self, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """将资金流向按日期 left-join 合并到K线DataFrame，缺失日期对应列为 NULL"""
        flow_cols = ["main_net_inflow", "super_large_net_inflow",
                     "large_net_inflow", "medium_net_inflow", "small_net_inflow"]
        ff = self._fetch_fund_flow(symbol)
        if ff.empty:
            for c in flow_cols:
                df[c] = None
            return df
        df = df.merge(ff, on="date", how="left")
        for c in flow_cols:
            if c not in df.columns:
                df[c] = None
        return df

    def _fetch_via_akshare(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """通过akshare获取数据"""
        import akshare as ak

        logger.info(f"通过akshare获取 {symbol} 数据...")
        try:
            df = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",  # 前复权
            )
            return df
        except Exception as e:
            logger.error(f"akshare获取数据失败: {e}")
            return pd.DataFrame()

    def _fetch_via_tushare(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """通过tushare获取数据"""
        if not self.config.tushare_token:
            raise ValueError("使用tushare数据源需要配置tushare_token")

        import tushare as ts

        logger.info(f"通过tushare获取 {symbol} 数据...")
        ts.set_token(self.config.tushare_token)
        pro = ts.pro_api()
        try:
            df = pro.daily(
                ts_code=f"{symbol}.SZ" if symbol.startswith("0") else f"{symbol}.SH",
                start_date=start_date,
                end_date=end_date,
            )
            # tushare列名映射
            df = df.rename(columns={
                "trade_date": "date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "vol": "volume",
                "amount": "amount",
            })
            return df
        except Exception as e:
            logger.error(f"tushare获取数据失败: {e}")
            return pd.DataFrame()

    def fetch_stocks(
        self,
        symbols: list[str],
        start_date: str,
        end_date: str,
        use_cache: bool = True,
        use_db: bool = True,
    ) -> dict[str, pd.DataFrame]:
        """
        获取多只股票的日线数据

        Args:
            symbols: 股票代码列表，如 ["000001", "600036"]
            start_date: 开始日期
            end_date: 结束日期
            use_cache: 是否使用本地CSV缓存（仅 use_db=False 时生效）
            use_db: 是否走数据库增量模式

        Returns:
            字典，key为股票代码，value为对应的DataFrame
        """
        result: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            try:
                result[symbol] = self.fetch_stock(symbol, start_date, end_date, use_cache, use_db)
            except Exception as e:
                logger.error(f"获取 {symbol} 数据失败: {e}")
                result[symbol] = pd.DataFrame()
        return result
