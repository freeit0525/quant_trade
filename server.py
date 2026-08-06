"""本地回测服务 - 托管前端页面并提供数据API

浏览器回测(backtest.html / macd.html)通过本服务拉取股票数据：
拉取流程走 DataFetcher(use_db=True)，实现"查库→增量拉取→入库→返回"，
即浏览器拉数据的同时自动入库，重复拉取只增量更新。

启动方式：
    python server.py [--port 8000]
然后浏览器访问 http://127.0.0.1:8000/ （入口页）
（也支持直接双击打开 index.html，此时前端自动指向本服务地址）
"""

import argparse
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from data.fetcher import DataFetcher
from utils.logger import get_logger

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent


def _json_default(obj):
    """json.dumps 兜底序列化：数据库返回的 Decimal 等类型转成可序列化类型"""
    import decimal

    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, (set, tuple)):
        return list(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _stock_name(code: str) -> str:
    """从本地数据库 stock_info 表查询股票名称，查不到返回空串"""
    from database.db import db_cursor

    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT name FROM market_data.stock_info WHERE symbol = %s",
                (code,),
            )
            row = cur.fetchone()
        return row[0] if row else ""
    except Exception as e:
        logger.warning("查询股票名称失败 %s: %s", code, e)
        return ""


def _secid(code: str) -> str:
    """根据股票代码推断交易所 secid 前缀（sh=1, sz/bj=0）"""
    if code.startswith(("6", "9", "11")):
        return f"1.{code}"
    return f"0.{code}"


class QuantHandler(SimpleHTTPRequestHandler):
    """静态文件 + 数据API 处理器"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    # ---------- 路由 ----------

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.handle_api(parsed)
        else:
            super().do_GET()

    # ---------- API ----------

    def handle_api(self, parsed: urlparse):
        """分发 /api/* 请求"""
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if parsed.path == "/api/kline":
                self.api_kline(params)
            elif parsed.path == "/api/search":
                self.api_search(params)
            elif parsed.path == "/api/stock_info":
                self.api_stock_info(params)
            elif parsed.path == "/api/sources":
                self.api_sources(params)
            elif parsed.path == "/api/fetch":
                self.api_fetch(params)
            else:
                self._send_json({"error": "未知接口"}, 404)
        except Exception as e:
            logger.exception("API处理失败: %s", parsed.path)
            self._send_json({"error": str(e)}, 500)

    def api_kline(self, params: dict):
        """拉取个股日线数据（查库→增量拉取→入库→返回）

        参数: code=股票代码, beg=开始日期YYYYMMDD, end=结束日期YYYYMMDD
        返回: { data: [{date,open,close,high,low,volume,amount,turnover,...}], stock: {code,name} }
        """
        code = (params.get("code") or "").strip()
        beg = (params.get("beg") or "").strip()
        end = (params.get("end") or "").strip()
        if not (code and beg and end):
            self._send_json({"error": "缺少参数 code/beg/end"}, 400)
            return

        logger.info("API /api/kline: code=%s beg=%s end=%s", code, beg, end)
        fetcher = DataFetcher()
        try:
            df = fetcher.fetch_stock(code, beg, end, use_db=True)
        except Exception as e:
            logger.exception("拉取 %s 失败: %s", code, e)
            self._send_json(
                {"error": f"拉取 {code} 数据失败（网络或数据源问题）: {e}"}, 500
            )
            return
        if df.empty:
            self._send_json(
                {
                    "error": (
                        f"未获取到 {code} 的K线数据。"
                        "若数据库无历史数据，请确认本地网络可访问数据源（东财/腾讯）"
                    )
                },
                404,
            )
            return

        records = df.to_dict(orient="records")
        for r in records:
            r["date"] = r["date"].strftime("%Y-%m-%d")
        self._send_json(
            {"data": records, "stock": {"code": code, "name": _stock_name(code)}},
            200,
        )

    def api_search(self, params: dict):
        """搜索A股代码/名称（本地数据库 stock_info 模糊查询）"""
        keyword = (params.get("keyword") or "").strip()
        if not keyword:
            self._send_json({"error": "缺少参数 keyword"}, 400)
            return

        from database.db import db_cursor

        with db_cursor() as cur:
            cur.execute(
                """SELECT symbol, name, market
                   FROM market_data.stock_info
                   WHERE symbol LIKE %s OR name LIKE %s
                   ORDER BY symbol
                   LIMIT 10""",
                (f"%{keyword}%", f"%{keyword}%"),
            )
            rows = cur.fetchall()

        stocks = [
            {"Code": r[0], "Name": r[1], "QuoteID": _secid(r[0])}
            for r in rows
        ]
        self._send_json({"data": stocks}, 200)

    def api_stock_info(self, params: dict):
        """查询股票基本信息（含上市日期，来自库表 stock_info）

        参数: code=股票代码
        返回: {symbol,name,market,list_date,industry}，list_date 为 'YYYY-MM-DD' 或 null
        """
        code = (params.get("code") or "").strip()
        if not code:
            self._send_json({"error": "缺少参数 code"}, 400)
            return

        from database.db import db_cursor

        with db_cursor() as cur:
            cur.execute(
                """SELECT symbol, name, market, list_date, industry
                   FROM market_data.stock_info WHERE symbol = %s""",
                (code,),
            )
            row = cur.fetchone()
        if row is None:
            self._send_json({"error": f"库中无 {code} 的股票信息（请先导入股票列表）"}, 404)
            return
        self._send_json({
            "symbol": row[0],
            "name": row[1],
            "market": row[2],
            "list_date": row[3].strftime("%Y-%m-%d") if row[3] else None,
            "industry": row[4],
        }, 200)

    def api_sources(self, params: dict):
        """探测各数据源连通性（并发请求，用 600000 近5日做轻量探测）

        返回: { data: [{key,name,type,desc,status,latency_ms,detail}, ...] }
        """
        from concurrent.futures import ThreadPoolExecutor

        keys = ["akshare", "eastmoney", "tencent", "sina", "tushare", "baostock", "tdx"]
        with ThreadPoolExecutor(max_workers=7) as pool:
            probes = dict(zip(keys, pool.map(self._probe_source, keys)))

        base = {
            "auto": {"key": "auto", "name": "自动选择", "type": "K线+资金流",
                     "desc": "akshare(东财)优先，失败自动回退东财直连→证券宝→腾讯", "status": "ok", "latency_ms": 0, "detail": ""},
            "akshare": {"key": "akshare", "name": "akshare", "type": "K线+资金流",
                        "desc": "akshare库抓取A股行情（数据源东财）", **probes["akshare"]},
            "eastmoney": {"key": "eastmoney", "name": "东方财富", "type": "K线",
                          "desc": "直连东财K线接口（不依赖akshare）", **probes["eastmoney"]},
            "tencent": {"key": "tencent", "name": "腾讯行情", "type": "K线",
                        "desc": "备用K线源（无资金流）", **probes["tencent"]},
            "sina": {"key": "sina", "name": "新浪财经", "type": "资金流",
                     "desc": "资金流补充（需先有K线）", **probes["sina"]},
            "tushare": {"key": "tushare", "name": "tushare", "type": "K线",
                        "desc": "专业数据源（需token）", **probes["tushare"]},
            "baostock": {"key": "baostock", "name": "证券宝", "type": "K线",
                         "desc": "免费稳定独立源（含换手率，无资金流）", **probes["baostock"]},
            "tdx": {"key": "tdx", "name": "通达信", "type": "分钟K线",
                    "desc": "分钟级K线（pytdx，供分钟MACD）", **probes["tdx"]},
        }
        self._send_json(
            {"data": [base[k] for k in ["auto", "akshare", "eastmoney", "tencent",
                                        "sina", "tushare", "baostock", "tdx"]]},
            200,
        )

    def _probe_source(self, key: str) -> dict:
        """探测单个数据源连通性（akshare 内部无超时控制，单独用线程做超时保护）"""
        if key == "akshare":
            return self._probe_akshare_limited()
        return self._probe_fast(key)

    def _probe_akshare_limited(self) -> dict:
        """akshare(东方财富)探测：东财域名在当前网络常被拦截且连接可能长时间挂起，
        用守护线程 + 8秒超时兜底，避免拖慢整体探测"""
        import threading
        import time

        result: dict = {}

        def worker():
            t0 = time.time()
            try:
                import akshare as ak
                df = ak.stock_zh_a_hist(
                    symbol="600000", period="daily",
                    start_date="20260801", end_date="20260806", adjust="qfq",
                )
                ok = df is not None and not df.empty
                result["status"] = "ok" if ok else "fail"
                result["latency_ms"] = int((time.time() - t0) * 1000)
                result["detail"] = "" if ok else "无数据返回"
            except Exception as e:
                result["status"] = "fail"
                result["latency_ms"] = int((time.time() - t0) * 1000)
                result["detail"] = f"{type(e).__name__}: {str(e)[:60]}"

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(8)
        if t.is_alive():
            return {"status": "fail", "latency_ms": 8000, "detail": "连接超时（>8秒）"}
        return result

    def _probe_fast(self, key: str) -> dict:
        """探测请求可控超时的数据源（腾讯/新浪/tushare/baostock/tdx）"""
        import time

        t0 = time.time()
        detail_hint = ""
        try:
            if key == "akshare":
                import akshare as ak
                df = ak.stock_zh_a_hist(
                    symbol="600000", period="daily",
                    start_date="20260801", end_date="20260806", adjust="qfq",
                )
                ok = df is not None and not df.empty
            elif key == "tencent":
                import requests
                r = requests.get(
                    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                    params={"param": "sh600000,day,2026-08-01,2026-08-06,10,qfq"},
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
                    timeout=8,
                )
                data = r.json().get("data", {}).get("sh600000", {})
                ok = bool(data.get("qfqday") or data.get("day"))
            elif key == "eastmoney":
                from data.fetcher import _em_get_with_retry
                r = _em_get_with_retry(
                    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                    params={"secid": "1.600000", "fields1": "f1,f2,f3",
                            "fields2": "f51,f52,f53,f54,f55,f56,f57",
                            "klt": "101", "fqt": "1", "beg": "20260801", "end": "20260806"},
                    retries=2, timeout=8,
                )
                ok = bool((r.json().get("data") or {}).get("klines"))
            elif key == "sina":
                import requests
                r = requests.get(
                    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_lscjfb",
                    params={"page": 1, "num": 1, "sort": "opendate", "asc": 0, "daima": "sh600000"},
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
                    timeout=8,
                )
                ok = bool(r.json())
            elif key == "tushare":
                from config.settings import Settings
                token = Settings().data_source.tushare_token
                if not token:
                    return {"status": "fail", "latency_ms": 0, "detail": "未配置 tushare_token"}
                import tushare as ts
                ts.set_token(token)
                df = ts.pro_api().daily(ts_code="600000.SH", start_date="20260801", end_date="20260806")
                ok = df is not None and not df.empty
            elif key == "baostock":
                from data.fetcher import probe_baostock
                ok, detail_hint = probe_baostock()
            elif key == "tdx":
                from data.fetcher import probe_tdx
                ok, detail_hint = probe_tdx()
            else:
                return {"status": "fail", "latency_ms": 0, "detail": f"未知数据源 {key}"}
        except Exception as e:
            return {"status": "fail", "latency_ms": int((time.time() - t0) * 1000),
                    "detail": f"{type(e).__name__}: {str(e)[:60]}"}
        return {"status": "ok" if ok else "fail", "latency_ms": int((time.time() - t0) * 1000),
                "detail": detail_hint or ("" if ok else "无数据返回")}

    def api_fetch(self, params: dict):
        """按指定数据源拉取股票数据并入库

        参数: source=auto|akshare|tencent|sina|tushare, code, beg, end（YYYYMMDD）
        - K线源(auto/akshare/tencent/tushare): 拉K线→合并资金流→入库→返回统计与预览
        - sina: 仅用新浪资金流更新库中已有K线的资金流字段
        """
        import pandas as pd
        from database.db import get_kline, get_stock_list_date, save_kline

        source = (params.get("source") or "auto").strip() or "auto"
        code = (params.get("code") or "").strip()
        beg = (params.get("beg") or "").strip()
        end = (params.get("end") or "").strip()
        if not code:
            self._send_json({"error": "缺少参数 code"}, 400)
            return
        # beg=all/空: 自动取库表上市日期作为起始（全量拉取上市至今）
        if not beg or beg.lower() == "all":
            list_date = get_stock_list_date(code)
            if not list_date:
                self._send_json(
                    {"error": f"库中无 {code} 的上市日期，无法全量拉取（可先运行导入股票信息脚本）"},
                    400,
                )
                return
            beg = list_date.replace("-", "")
        if not end:
            self._send_json({"error": "缺少参数 end"}, 400)
            return

        logger.info("API /api/fetch: source=%s code=%s beg=%s end=%s", source, code, beg, end)
        fetcher = DataFetcher()

        # ---- 新浪: 仅资金流 ----
        if source == "sina":
            ff = fetcher._fetch_fund_flow_via_sina(code)
            if ff.empty:
                self._send_json({"error": f"新浪接口未获取到 {code} 资金流数据"}, 404)
                return
            kline = get_kline(code, "1900-01-01", "2100-01-01")
            if kline.empty:
                self._send_json({"error": f"库中暂无 {code} 的K线，请先用K线接口拉取"}, 400)
                return
            ff["date"] = pd.to_datetime(ff["date"])
            merged = kline[["date"]].merge(ff, on="date", how="left")
            kline = kline.drop(columns=[c for c in ff.columns if c != "date"])
            merged = pd.concat([kline, merged.drop(columns=["date"])], axis=1)
            saved = save_kline(merged, code)
            # 统一返回统计与预览（范围取库中全部记录）
            full = get_kline(code, "1900-01-01", "2100-01-01")
            preview = full.head(5).to_dict(orient="records")
            for r in preview:
                r["date"] = r["date"].strftime("%Y-%m-%d")
            self._send_json(
                {"ok": True, "source": "sina", "code": code, "name": _stock_name(code),
                 "list_date": get_stock_list_date(code),
                 "count": len(full),
                 "first": full["date"].min().strftime("%Y-%m-%d"),
                 "last": full["date"].max().strftime("%Y-%m-%d"),
                 "last_close": float(full.iloc[-1]["close"]) if not full.empty else None,
                 "saved": saved, "preview": preview,
                 "message": f"资金流已更新 {saved} 条（{ff['date'].min().date()} ~ {ff['date'].max().date()}）"},
                200,
            )
            return

        # ---- K线源 ----
        try:
            if source == "auto":
                df = fetcher.fetch_stock(code, beg, end, use_db=True)
            elif source == "akshare":
                df = fetcher._fetch_via_akshare(code, beg, end)
            elif source == "eastmoney":
                df = fetcher._fetch_via_eastmoney(code, beg, end)
            elif source == "baostock":
                df = fetcher._fetch_via_baostock(code, beg, end)
            elif source == "tencent":
                df = fetcher._fetch_via_tencent(code, beg, end)
            elif source == "tushare":
                df = fetcher._fetch_via_tushare(code, beg, end)
            else:
                self._send_json({"error": f"未知数据源: {source}"}, 400)
                return
        except Exception as e:
            logger.exception("拉取 %s 失败: %s", code, e)
            self._send_json({"error": f"拉取失败（{source}）: {e}"}, 500)
            return

        if df.empty:
            self._send_json({"error": f"数据源 {source} 未获取到 {code} 的K线数据"}, 404)
            return

        # 非auto源: 标准化→计算量比→合并资金流→入库
        saved = 0
        if source != "auto":
            df = fetcher._normalize_columns(df).sort_values("date").reset_index(drop=True)
            df = fetcher._calc_volume_ratio(df, code, beg.replace("-", ""))
            df = fetcher._merge_fund_flow(df, code)
            saved = save_kline(df, code)

        # 从库返回统计与预览
        full = get_kline(code, beg, end)
        if full.empty:
            self._send_json({"error": f"入库后仍未获取到 {code} 的K线数据"}, 404)
            return
        preview = full.head(5).to_dict(orient="records")
        for r in preview:
            r["date"] = r["date"].strftime("%Y-%m-%d")
        self._send_json(
            {"ok": True, "source": source, "code": code, "name": _stock_name(code),
             "list_date": get_stock_list_date(code),
             "count": len(full),
             "first": full["date"].min().strftime("%Y-%m-%d"),
             "last": full["date"].max().strftime("%Y-%m-%d"),
             "last_close": float(full.iloc[-1]["close"]),
             "saved": saved, "preview": preview},
            200,
        )

    # ---------- 工具 ----------

    def _send_cors_headers(self):
        """允许 file:// 页面或任意来源跨域访问 API"""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        """预检请求处理"""
        self.send_response(204)
        self._send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, obj: dict, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)


def main():
    parser = argparse.ArgumentParser(description="本地量化回测服务")
    parser.add_argument("--port", type=int, default=8000, help="服务端口，默认 8000")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), QuantHandler)
    print(f"量化工具服务已启动: http://127.0.0.1:{args.port}/")
    print(f"功能入口: /（index.html）, /backtest.html, /macd.html")
    print(f"数据API: /api/kline, /api/search （浏览器拉数据时自动入库）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()
