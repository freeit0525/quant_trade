"""数据获取模块 - 支持通过akshare获取A股日线数据"""

import os
import re
from pathlib import Path
from typing import Optional

import pandas as pd

from config.settings import DataSourceConfig
from utils.logger import get_logger

logger = get_logger(__name__)

# 东财接口候选主机：被风控时不同主机间歇放行，逐主/机轮询提高成功率
_EM_API_HOSTS = ("push2.eastmoney.com", "82.push2.eastmoney.com", "push2his.eastmoney.com")

# 东方财富接口请求头：模拟完整浏览器指纹，降低被服务端风控按 UA 拒绝的概率
_EM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}


# MACD 参数组：键为列名后缀（空=默认组，列名 macd_dif/dea/hist），值为 (快线, 慢线, 信号)
MACD_GROUPS = {
    "": (12, 26, 9),        # 默认组
    "_6_12_5": (6, 12, 5),
    "_3_8_3": (3, 8, 3),
    "_10_20_7": (10, 20, 7),
}


# 东财请求会话（curl_cffi Chrome 指纹）：进程内复用连接；初始化失败时回退 requests
_EM_SESSION = None
_EM_SESSION_WARMED = False


def _em_session():
    """懒加载东财请求会话（curl_cffi impersonate=chrome，模拟浏览器 TLS/HTTP2 指纹）

    Returns:
        可用会话对象；curl_cffi 不可用/初始化失败时返回 None（调用方回退 requests）
    """
    global _EM_SESSION, _EM_SESSION_WARMED
    if _EM_SESSION is not None:
        return _EM_SESSION
    try:
        from curl_cffi import requests as curl_requests

        s = curl_requests.Session(impersonate="chrome", timeout=15)
        # Cookie 预热：先访问东财首页拿 cookie，降低被按"无 cookie 程序化请求"拦截的概率
        if not _EM_SESSION_WARMED:
            try:
                s.get("https://quote.eastmoney.com/", timeout=8)
            except Exception:
                pass
            _EM_SESSION_WARMED = True
        _EM_SESSION = s
        logger.info("东财请求已启用 curl_cffi Chrome 指纹会话")
    except Exception as e:
        logger.warning(f"curl_cffi 初始化失败，东财请求回退 requests: {e}")
        _EM_SESSION = None
    return _EM_SESSION


def _em_get_with_retry(
    url: str,
    params: dict,
    retries: int = 3,
    timeout: int = 15,
    retry_interval: float = 1.0,
):
    """GET 东方财富接口：Chrome指纹(curl_cffi) + 固定Session + 浏览器头 + 失败重试

    - curl_cffi 模拟 Chrome 的 TLS/HTTP2 指纹，显著降低被 WAF 识别为程序化请求的概率
    - Session 进程内复用，避免每次新建 TCP 连接
    - 保留线性退避重试；curl_cffi 不可用时自动回退 requests
    """
    import time

    import requests

    session = _em_session()
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            if session is not None:
                resp = session.get(url, params=params, headers=_EM_HEADERS, timeout=timeout)
            else:
                resp = requests.get(url, params=params, headers=_EM_HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_exc = e
            logger.warning(
                "东财接口请求失败(第%d/%d次): %s %s", attempt, retries, url, e
            )
            if attempt < retries:
                time.sleep(retry_interval * attempt)  # 退避 1s, 2s, ...
    assert last_exc is not None
    raise last_exc


# ---------- 证券宝(baostock) ----------
_bs_logged_in = False  # 进程内登录一次，避免每次拉取重复连接

_BS_KLINE_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,turn,tradestatus,pctChg,isST"
)


def _baostock_ensure_login() -> bool:
    """登录证券宝（进程内仅登录一次），成功返回 True"""
    global _bs_logged_in
    if _bs_logged_in:
        return True
    try:
        import baostock as bs

        lg = bs.login()
    except Exception as e:
        logger.warning(f"baostock 登录异常: {e}")
        return False
    if lg.error_code == "0":
        _bs_logged_in = True
        return True
    logger.warning(f"baostock 登录失败: {lg.error_msg}")
    return False


def probe_baostock(limit: float = 8.0) -> tuple[bool, str]:
    """探测证券宝连通性（baostock 内部连接无超时控制，用线程做超时兜底）"""
    import threading
    import time

    result: dict = {}

    def worker():
        if not _baostock_ensure_login():
            result["ok"] = False
            result["detail"] = "登录失败"
            return
        try:
            import baostock as bs

            rs = bs.query_history_k_data_plus(
                "sh.600000", "date,close",
                start_date="2026-08-01", end_date="2026-08-06",
                frequency="d", adjustflag="2",
            )
            ok = rs.error_code == "0" and rs.next()
            result["ok"] = bool(ok)
            result["detail"] = "" if ok else rs.error_msg
        except Exception as e:
            result["ok"] = False
            result["detail"] = f"{type(e).__name__}: {str(e)[:60]}"

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(limit)
    if t.is_alive():
        return False, "连接超时（>8秒）"
    return bool(result.get("ok")), str(result.get("detail", ""))


# ---------- 通达信(pytdx) ----------
# 实测可用的通达信行情服务器优先，再回退 pytdx 官方服务器列表
_TDX_PREFERRED_HOSTS = [
    ("180.153.18.170", 7709),
    ("123.125.108.14", 7709),
    ("218.6.170.47", 7709),
    ("115.238.56.198", 7709),
    ("180.153.39.51", 7709),
    ("122.51.120.58", 7709),
]

# pytdx get_security_bars 的 category: 1/5/15/30/60 分钟
_TDX_PERIOD_MAP = {"1": 7, "5": 0, "15": 1, "30": 2, "60": 3}


def _tdx_hosts() -> list[tuple[str, int]]:
    """生成通达信服务器候选列表（去重）：首选服务器 + 官方列表"""
    from pytdx.config.hosts import hq_hosts

    candidates = list(_TDX_PREFERRED_HOSTS)
    for h in hq_hosts:
        try:
            candidates.append((h[1], int(h[2])))
        except (IndexError, ValueError):
            candidates.append((h[0], int(h[1])))
    seen: set[tuple[str, int]] = set()
    return [x for x in candidates if not (x in seen or seen.add(x))]


def probe_tdx() -> tuple[bool, str]:
    """探测通达信连通性：依次连接候选服务器，取到任意K线即成功"""
    from pytdx.hq import TdxHq_API

    for host, port in _tdx_hosts():
        try:
            api = TdxHq_API()
            if api.connect(host, port, time_out=4):
                bars = api.get_security_bars(4, 1, "600000", 0, 1)  # 日线
                api.disconnect()
                if bars:
                    return True, f"{host}:{port}"
        except Exception:
            continue
    return False, "无可用服务器"


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

        df = self._fetch_from_source(symbol, start_date, end_date)

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
        """数据库增量拉取：先查库中已有数据范围（最小/最大日期），
        仅拉取缺失缺口（历史缺口 + 尾部缺口）入库，再从库返回完整范围"""
        from database.db import get_kline, get_kline_max_date, get_kline_min_date, save_kline

        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        start_str = start_dt.strftime("%Y-%m-%d")
        end_str = end_dt.strftime("%Y-%m-%d")

        # 1. 查询库中已有数据范围，确定需要拉取的缺口
        max_date_str = get_kline_max_date(symbol)
        min_date_str = get_kline_min_date(symbol)

        gaps: list[tuple[str, str]] = []  # (fetch_start, fetch_end)，均为 YYYYMMDD
        if max_date_str is None:
            # 库中无数据，全量拉取
            gaps.append((start_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d")))
            logger.info(f"数据库无 {symbol} 数据，全量拉取 {start_str} ~ {end_str}")
        else:
            max_dt = pd.to_datetime(max_date_str)
            min_dt = pd.to_datetime(min_date_str)
            # 尾部缺口：库中最大日期 < 请求结束日期
            if max_dt < end_dt:
                fetch_start = (max_dt + pd.Timedelta(days=1)).strftime("%Y%m%d")
                gaps.append((fetch_start, end_dt.strftime("%Y%m%d")))
                logger.info(f"数据库已有 {symbol} 至 {max_date_str}，增量拉取 {fetch_start} ~ {end_str}")
            # 头部缺口（历史缺口）：库中最小日期 > 请求开始日期
            if min_dt > start_dt:
                fetch_end = (min_dt - pd.Timedelta(days=1)).strftime("%Y%m%d")
                gaps.append((start_dt.strftime("%Y%m%d"), fetch_end))
                logger.info(f"数据库已有 {symbol} 自 {min_date_str}，补拉历史缺口 {start_str} ~ {fetch_end}")

        # 2. 依次拉取各缺口并入库
        fetched_dfs = []
        for fetch_start, fetch_end in gaps:
            df = self._fetch_from_source(symbol, fetch_start, fetch_end)
            if df.empty:
                logger.warning(f"未获取到数据: {symbol} ({fetch_start} ~ {fetch_end})")
                continue
            df = self._normalize_columns(df)
            df = df.sort_values("date").reset_index(drop=True)
            # 计算量比（需库中 fetch_start 之前最近5个交易日的 volume 作为窗口前置）
            df = self._calc_volume_ratio(df, symbol, fetch_start)
            # 计算 MACD（拼接库中全量 close 重算，保证增量精确）
            df = self._calc_macd(df, symbol)
            # 计算 MA/RSI/KDJ（拼接库中全量 OHLC 重算，保证增量精确）
            df = self._calc_tech_indicators(df, symbol)
            # 合并资金流向（近100天可获取，更早为 NULL）
            df = self._merge_fund_flow(df, symbol)
            fetched_dfs.append(df)

        if fetched_dfs:
            combined = pd.concat(fetched_dfs, ignore_index=True)
            save_kline(combined, symbol)

        # 3. 从数据库返回完整范围
        return get_kline(symbol, start_str, end_str)

    def _fetch_from_source(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """按配置的数据源拉取数据；东财系(akshare/eastmoney)失败时逐级回退 baostock → 腾讯"""
        if self.config.source == "akshare":
            df = self._fetch_via_akshare(symbol, start_date, end_date)
            if df.empty:
                logger.warning(f"akshare获取失败，回退东财直连: {symbol}")
                df = self._fetch_via_eastmoney(symbol, start_date, end_date)
            if df.empty:
                logger.warning(f"东财直连获取失败，回退证券宝: {symbol}")
                df = self._fetch_via_baostock(symbol, start_date, end_date)
            if df.empty:
                logger.warning(f"证券宝获取失败，回退腾讯数据源: {symbol}")
                df = self._fetch_via_tencent(symbol, start_date, end_date)
        elif self.config.source == "eastmoney":
            df = self._fetch_via_eastmoney(symbol, start_date, end_date)
            if df.empty:
                logger.warning(f"东财直连获取失败，回退证券宝: {symbol}")
                df = self._fetch_via_baostock(symbol, start_date, end_date)
            if df.empty:
                logger.warning(f"证券宝获取失败，回退腾讯数据源: {symbol}")
                df = self._fetch_via_tencent(symbol, start_date, end_date)
        elif self.config.source == "baostock":
            df = self._fetch_via_baostock(symbol, start_date, end_date)
            if df.empty:
                logger.warning(f"证券宝获取失败，回退腾讯数据源: {symbol}")
                df = self._fetch_via_tencent(symbol, start_date, end_date)
        elif self.config.source == "tushare":
            df = self._fetch_via_tushare(symbol, start_date, end_date)
        else:
            raise ValueError(f"不支持的数据源: {self.config.source}")
        return df

    def _calc_volume_ratio(self, df: pd.DataFrame, symbol: str, fetch_start: str) -> pd.DataFrame:
        """计算量比 = 当日成交量 / 过去5个交易日（不含当日）平均成交量

        增量拉取时需拼接库中 fetch_start 之前最近5个交易日的 volume 作为滚动窗口前置，
        否则新拉取的前几行量比会因窗口不足而缺失。
        前期数据不足时失真补全：第1天=当日量/当日量=1，第2天=当日/前1日均量，
        第3天=当日/前2日均量……第6天起为标准5日均量。
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

        # 过去N日均量（不含当日，窗口随前期数据逐步扩大，min_periods=1），
        # 首日无前一日均量时用当日量兜底 -> 量比=1
        past_avg = combined["volume"].rolling(5, min_periods=1).mean().shift(1)
        combined["volume_ratio"] = (
            combined["volume"] / past_avg.fillna(combined["volume"])
        )
        ratio_map = dict(zip(combined["date"], combined["volume_ratio"]))
        df["volume_ratio"] = df["date"].map(ratio_map)
        return df

    def _calc_macd(self, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """计算 MACD（多组参数，见 MACD_GROUPS），增量拉取时拼接库中全量 close 重算保证精确

        MACD 是全局滚动指标（EMA 自首日累积），增量更新时必须用库中已有历史 close
        作为前置；拼接后整体重算，仅把本次 df 覆盖日期对应的结果写回。
        K线数不足该组慢线周期(slow)时，该组列置空。
        """
        if df.empty or "close" not in df.columns:
            return df

        from database.db import get_kline

        min_date = pd.to_datetime(df["date"].min())
        pre_end = (min_date - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        pre = get_kline(symbol, "1990-01-01", pre_end)
        parts = []
        if not pre.empty:
            parts.append(pre[["date", "close"]])
        parts.append(df[["date", "close"]])
        all_close = (
            pd.concat(parts, ignore_index=True)
            .drop_duplicates("date", keep="last")
            .sort_values("date")
            .reset_index(drop=True)
        )
        close = all_close["close"]
        for suffix, (fast, slow, signal) in MACD_GROUPS.items():
            if len(close) < slow:
                df[f"macd_dif{suffix}"] = None
                df[f"macd_dea{suffix}"] = None
                df[f"macd_hist{suffix}"] = None
                continue
            ema_fast = close.ewm(span=fast, adjust=False).mean()
            ema_slow = close.ewm(span=slow, adjust=False).mean()
            dif = ema_fast - ema_slow
            dea = dif.ewm(span=signal, adjust=False).mean()
            hist = (dif - dea) * 2
            all_close[f"macd_dif{suffix}"] = dif
            all_close[f"macd_dea{suffix}"] = dea
            all_close[f"macd_hist{suffix}"] = hist
        cols = (
            ["date"]
            + [c for s in MACD_GROUPS for c in (f"macd_dif{s}", f"macd_dea{s}", f"macd_hist{s}")]
        )
        return df.merge(all_close[cols], on="date", how="left")

    def _calc_tech_indicators(self, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """计算 MA(5/10/20/30/60/120)、RSI(14)、KDJ(9,3,3)，增量时拼接库中全量 OHLC 重算

        与 _calc_macd 相同策略：滚动指标需用库中已有历史做前置，
        拼接后整体重算，仅把本次 df 覆盖日期的结果写回。
        MA 需 n 根K线起步；RSI(14) 用 Wilder 平滑；KDJ(9,3,3) 窗口不足时为 NULL。
        """
        if df.empty or "close" not in df.columns:
            return df

        from database.db import get_kline

        min_date = pd.to_datetime(df["date"].min())
        pre_end = (min_date - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        pre = get_kline(symbol, "1990-01-01", pre_end)
        parts = []
        if not pre.empty:
            parts.append(pre[["date", "high", "low", "close"]])
        parts.append(df[["date", "high", "low", "close"]])
        all_ohlc = (
            pd.concat(parts, ignore_index=True)
            .drop_duplicates("date", keep="last")
            .sort_values("date")
            .reset_index(drop=True)
        )
        close = all_ohlc["close"]

        # MA：简单移动平均（前期窗口不足时用可用窗口，min_periods=1 失真补全）
        ma_windows = (5, 10, 20, 30, 60, 120)
        for n in ma_windows:
            all_ohlc[f"ma{n}"] = close.rolling(n, min_periods=1).mean()

        # RSI(14)：Wilder 平滑（首根及完全横盘无涨跌时失真补 50）
        period = 14
        delta = close.diff().fillna(0)
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - 100 / (1 + rs)
        all_ohlc["rsi14"] = rsi.fillna(50)

        # KDJ(9,3,3)：RSV -> K(1/3平滑) -> D(1/3平滑) -> J
        # 前期窗口不足时用可用窗口(min_periods=1)；HH=LL 无波动时 RSV 补 50（失真补全）
        low9 = all_ohlc["low"].rolling(9, min_periods=1).min()
        high9 = all_ohlc["high"].rolling(9, min_periods=1).max()
        rsv = ((close - low9) / (high9 - low9) * 100).fillna(50)
        k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
        d = k.ewm(alpha=1 / 3, adjust=False).mean()
        all_ohlc["kdj_k"] = k
        all_ohlc["kdj_d"] = d
        all_ohlc["kdj_j"] = 3 * k - 2 * d

        # BIAS(5,10,20)：乖离率 = (close - MA(n)) / MA(n) * 100（MA 用可用窗口）
        for n in (5, 10, 20):
            ma_n = close.rolling(n, min_periods=1).mean()
            all_ohlc[f"bias{n}"] = (close - ma_n) / ma_n * 100

        keep = (
            ["date"]
            + [f"ma{n}" for n in ma_windows]
            + ["rsi14", "kdj_k", "kdj_d", "kdj_j", "bias5", "bias10", "bias20"]
        )
        return df.merge(all_ohlc[keep], on="date", how="left")

    def _infer_market_code(self, symbol: str) -> str:
        """根据股票代码推断交易所代码（sh/sz/bj），用于资金流向接口"""
        if symbol.startswith("6") or symbol.startswith("9"):
            return "sh"
        if symbol.startswith("0") or symbol.startswith("3"):
            return "sz"
        return "bj"

    def _fetch_fund_flow(self, symbol: str) -> pd.DataFrame:
        """获取个股资金流向，返回标准化DataFrame（date + 5个净流入列，单位元）

        优先 akshare(东方财富)，失败时回退新浪 lscjfb 接口。
        """
        df = self._fetch_fund_flow_via_akshare(symbol)
        if df.empty:
            logger.warning(f"akshare资金流向不可用，回退新浪数据源: {symbol}")
            df = self._fetch_fund_flow_via_sina(symbol)
        return df

    def _fetch_fund_flow_via_akshare(self, symbol: str) -> pd.DataFrame:
        """通过 akshare 获取个股资金流向（近100个交易日），返回标准化DataFrame"""
        try:
            import akshare as ak
        except ImportError:
            return pd.DataFrame()

        market = self._infer_market_code(symbol)
        import time

        ff = None
        for attempt in range(1, 3):  # akshare内部走东财接口，无请求头控制，失败自动重试1次
            try:
                ff = ak.stock_individual_fund_flow(stock=symbol, market=market)
                break
            except Exception as e:
                logger.warning(f"akshare资金流向获取失败 {symbol}(第{attempt}次): {e}")
                if attempt < 2:
                    time.sleep(1)
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

    def _fetch_fund_flow_via_sina(self, symbol: str) -> pd.DataFrame:
        """通过新浪资金流向历史接口获取分单净流入（备用数据源）

        接口: MoneyFlow.ssl_qsfx_lscjfb，返回近约8年数据
        字段映射（单位: 元）:
            r0_net 超大单净流入, r1_net 大单, r2_net 中单, r3_net 小单
            主力净流入 = 超大单 + 大单（r0_net + r1_net）
        """
        import requests

        daima = f"{self._infer_market_code(symbol)}{symbol}"
        logger.info(f"通过新浪资金流向获取 {daima} 数据...")
        try:
            url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_lscjfb"
            resp = requests.get(
                url,
                params={"page": 1, "num": 2000, "sort": "opendate", "asc": 0, "daima": daima},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Referer": "https://finance.sina.com.cn/",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                logger.warning(f"新浪资金流向无 {daima} 数据")
                return pd.DataFrame()

            rows = []
            for row in data:
                try:
                    rows.append({
                        "date": pd.to_datetime(row["opendate"]),
                        "main_net_inflow": float(row["r0_net"]) + float(row["r1_net"]),
                        "super_large_net_inflow": float(row["r0_net"]),
                        "large_net_inflow": float(row["r1_net"]),
                        "medium_net_inflow": float(row["r2_net"]),
                        "small_net_inflow": float(row["r3_net"]),
                    })
                except (KeyError, ValueError, TypeError):
                    continue
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows)
            df = df.sort_values("date").reset_index(drop=True)
            return df
        except Exception as e:
            logger.error(f"新浪资金流向获取失败: {e}")
            return pd.DataFrame()

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
        """通过akshare获取数据（内部走东财接口，无法注入请求头；失败自动重试1次）"""
        import time

        import akshare as ak

        logger.info(f"通过akshare获取 {symbol} 数据...")
        last_exc: Exception | None = None
        for attempt in range(1, 3):
            try:
                df = ak.stock_zh_a_hist(
                    symbol=symbol,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust="qfq",  # 前复权
                )
                if df is not None and not df.empty:
                    return df
                last_exc = RuntimeError("akshare返回空数据")
            except Exception as e:
                last_exc = e
                logger.warning(f"akshare获取 {symbol} 失败(第{attempt}次): {e}")
            if attempt < 2:
                time.sleep(1)
        logger.error(f"akshare获取数据失败: {last_exc}")
        return pd.DataFrame()

    def _fetch_via_eastmoney(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """直连东方财富行情接口获取前复权日K线（不依赖akshare库）

        自动带浏览器请求头并失败重试（见 _em_get_with_retry），
        返回列: date/open/close/high/low/volume(股)/amount(元)/amplitude(%)/pct_change(%)/change(元)/turnover(%)
        """
        secid = f"1.{symbol}" if symbol.startswith(("6", "9")) else f"0.{symbol}"
        logger.info(f"直连东方财富获取 {secid} 数据...")
        try:
            url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
            params = {
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101",        # 日线
                "fqt": "1",          # 前复权
                "beg": start_date,
                "end": end_date,
            }
            resp = _em_get_with_retry(url, params=params, timeout=15)
            data = resp.json().get("data") or {}
            klines = data.get("klines") or []
            if not klines:
                logger.warning(f"东方财富无 {secid} 数据")
                return pd.DataFrame()

            # klines 每行格式: 日期,开,收,高,低,量(手),额(元),振幅%,涨跌幅%,涨跌额,换手率%
            rows = []
            for line in klines:
                p = line.split(",")
                if len(p) < 11:
                    continue
                try:
                    rows.append({
                        "date": pd.to_datetime(p[0]),
                        "open": float(p[1]),
                        "close": float(p[2]),
                        "high": float(p[3]),
                        "low": float(p[4]),
                        "volume": float(p[5]) * 100,  # 手 -> 股
                        "amount": float(p[6]),
                        "amplitude": float(p[7]) if p[7] else None,
                        "pct_change": float(p[8]) if p[8] else None,
                        "change": float(p[9]) if p[9] else None,
                        "turnover": float(p[10]) if p[10] else None,
                    })
                except (ValueError, TypeError, IndexError):
                    continue
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows)
            df = df.sort_values("date").reset_index(drop=True)
            return df
        except Exception as e:
            logger.error(f"东方财富直连接口获取数据失败: {e}")
            return pd.DataFrame()

    def _fetch_via_baostock(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """通过证券宝(baostock)获取前复权日K线（独立数据源，免费稳定）

        返回列: date/open/close/high/low/volume(股)/amount(元)/amplitude(%)/pct_change(%)/change(元)/turnover(%)
        """
        if not _baostock_ensure_login():
            return pd.DataFrame()
        import baostock as bs

        bs_code = f"sh.{symbol}" if symbol.startswith(("6", "9")) else f"sz.{symbol}"
        logger.info(f"通过证券宝获取 {bs_code} 数据...")
        fmt = lambda s: f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        try:
            rs = bs.query_history_k_data_plus(
                bs_code,
                _BS_KLINE_FIELDS,
                start_date=fmt(start_date),
                end_date=fmt(end_date),
                frequency="d",
                adjustflag="2",  # 2=前复权
            )
            if rs.error_code != "0":
                logger.warning(f"证券宝查询失败 {bs_code}: {rs.error_msg}")
                return pd.DataFrame()
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
        except Exception as e:
            logger.error(f"证券宝获取数据失败: {e}")
            return pd.DataFrame()
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=rs.fields)
        df["date"] = pd.to_datetime(df["date"])
        for c in ("open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pctChg"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["change"] = df["close"] - df["preclose"]
        df["amplitude"] = (df["high"] - df["low"]) / df["preclose"] * 100
        df = df.rename(columns={"pctChg": "pct_change", "turn": "turnover"})
        keep = ["date", "open", "close", "high", "low", "volume", "amount",
                "amplitude", "pct_change", "change", "turnover"]
        return df[[c for c in keep if c in df.columns]].sort_values("date").reset_index(drop=True)

    def _tdx_connect(self):
        """连接通达信行情服务器（pytdx），返回已连接的 API 实例，失败返回 None"""
        from pytdx.hq import TdxHq_API

        for host, port in _tdx_hosts():
            try:
                api = TdxHq_API()
                if api.connect(host, port, time_out=5):
                    if api.get_security_bars(4, 1, "600000", 0, 1):  # 用日线验证连通
                        logger.info(f"通达信行情服务器: {host}:{port}")
                        return api
                    api.disconnect()
            except Exception:
                continue
        logger.error("通达信行情服务器连接失败")
        return None

    def fetch_minute_bars(self, symbol: str, period: str = "5", count: int = 240) -> pd.DataFrame:
        """通过通达信(pytdx)获取分钟K线，供分钟级MACD等使用

        Args:
            symbol: 股票代码
            period: 1/5/15/30/60（分钟）
            count: 返回K线根数（单次最多约800根）

        Returns:
            DataFrame: datetime/open/close/high/low/volume(股)/amount(元)
        """
        category = _TDX_PERIOD_MAP.get(str(period))
        if category is None:
            logger.error(f"不支持的分钟周期: {period}")
            return pd.DataFrame()
        api = self._tdx_connect()
        if api is None:
            return pd.DataFrame()
        try:
            market = 1 if symbol.startswith(("6", "9")) else 0
            bars = api.get_security_bars(category, market, symbol, 0, count)
            if not bars:
                logger.warning(f"通达信无 {symbol} {period}分钟K线")
                return pd.DataFrame()
            df = pd.DataFrame(bars)
            # 通达信空数据以极小值填充，过滤无意义的行
            df = df[df["vol"] >= 1]
            if df.empty:
                return pd.DataFrame()
            df["datetime"] = pd.to_datetime(df["datetime"])
            df = df.rename(columns={"vol": "volume"})
            df = df.sort_values("datetime").reset_index(drop=True)
            keep = ["datetime", "open", "close", "high", "low", "volume", "amount"]
            return df[[c for c in keep if c in df.columns]]
        finally:
            try:
                api.disconnect()
            except Exception:
                pass

    def _fetch_via_tencent(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """通过腾讯行情接口获取前复权日线数据（备用数据源）

        返回列: date/open/close/high/low/volume(股)/amount(可算)/amplitude/pct_change/change
        turnover(换手率) 腾讯接口不提供，置为 None。
        """
        import requests

        # 腾讯接口代码前缀: 沪市 sh、深市 sz、北交所 bj
        market = self._infer_market_code(symbol)
        qq_symbol = f"{market}{symbol}"
        logger.info(f"通过腾讯行情获取 {qq_symbol} 数据...")
        try:
            url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            # 腾讯接口需要 "YYYY-MM-DD" 格式日期
            fmt = lambda s: f"{s[:4]}-{s[4:6]}-{s[6:8]}"
            params = {
                "param": f"{qq_symbol},day,{fmt(start_date)},{fmt(end_date)},640,qfq",
            }
            resp = requests.get(
                url,
                params=params,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Referer": "https://gu.qq.com/",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {}).get(qq_symbol, {})
            klines = data.get("qfqday") or data.get("day")
            if not klines:
                logger.warning(f"腾讯行情无 {qq_symbol} 数据")
                return pd.DataFrame()

            rows = []
            for k in klines:
                # 格式: [日期, 开, 收, 高, 低, 成交量(手)]
                if len(k) < 6:
                    continue
                try:
                    open_p, close_p, high_p, low_p = map(float, k[1:5])
                    rows.append({
                        "date": pd.to_datetime(k[0]),
                        "open": open_p,
                        "close": close_p,
                        "high": high_p,
                        "low": low_p,
                        "volume": float(k[5]) * 100,  # 手 -> 股
                    })
                except (ValueError, TypeError):
                    continue

            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows)
            df = df.sort_values("date").reset_index(drop=True)

            # 计算派生字段
            df["pct_change"] = df["close"].pct_change() * 100
            df["change"] = df["close"].diff()
            df["amplitude"] = (df["high"] - df["low"]) / df["close"].shift(1) * 100
            df["amount"] = df["volume"] * df["close"]  # 近似成交额(元)
            df["turnover"] = None
            return df
        except Exception as e:
            logger.error(f"腾讯行情获取数据失败: {e}")
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
