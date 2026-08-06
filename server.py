"""本地回测服务 - 托管前端页面并提供数据API

浏览器回测(backtest.html / macd.html)通过本服务拉取股票数据：
拉取流程走 DataFetcher(use_db=True)，实现"查库→增量拉取→入库→返回"，
即浏览器拉数据的同时自动入库，重复拉取只增量更新。

启动方式：
    python server.py [--port 8000]
然后浏览器访问 http://127.0.0.1:8000/backtest.html
（也支持直接双击打开 backtest.html，此时前端自动指向本服务地址）
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
    print(f"回测服务已启动: http://127.0.0.1:{args.port}/backtest.html")
    print(f"数据API: /api/kline, /api/search （浏览器拉数据时自动入库）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()
