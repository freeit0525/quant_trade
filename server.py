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
import math
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


def _sanitize(obj):
    """递归将 NaN/Infinity 等非有限浮点值转为 None，保证 JSON 合法（JSON 不允许 NaN）"""
    import math

    if isinstance(obj, float):
        return None if not math.isfinite(obj) else obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def _same_value(a, b):
    """两个值是否视为相同（NaN 视为相等，用于刷新前后对比）"""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        if math.isnan(float(a)) and math.isnan(float(b)):
            return True
    except (TypeError, ValueError):
        pass
    return a == b


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


def _opt_float(x):
    """把可选数值参数转成 float，空/非法返回 None"""
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


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

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.handle_api_post(parsed)
        else:
            self._send_json({"error": "未知接口"}, 404)

    # ---------- API ----------

    def handle_api(self, parsed: urlparse):
        """分发 /api/* GET 请求"""
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if parsed.path == "/api/kline":
                self.api_kline(params)
            elif parsed.path == "/api/kline_db":
                self.api_kline_db(params)
            elif parsed.path == "/api/index_kline":
                self.api_index_kline(params)
            elif parsed.path == "/api/stock_list":
                self.api_stock_list(params)
            elif parsed.path == "/api/stock_catalog":
                self.api_stock_catalog(params)
            elif parsed.path == "/api/search":
                self.api_search(params)
            elif parsed.path == "/api/stock_info":
                self.api_stock_info(params)
            elif parsed.path == "/api/sources":
                self.api_sources(params)
            elif parsed.path == "/api/fetch":
                self.api_fetch(params)
            elif parsed.path == "/api/refresh_today":
                self.api_refresh_today(params)
            elif parsed.path == "/api/probe":
                self.api_probe(params)
            elif parsed.path == "/api/strategies":
                self.api_strategies(params)
            elif parsed.path == "/api/backtest/results":
                self.api_backtest_results(params)
            elif parsed.path == "/api/predictions":
                self.api_predictions(params)
            elif parsed.path == "/api/predictions/stats":
                self.api_predictions_stats(params)
            elif parsed.path == "/api/backfill/status":
                self.api_backfill_status(params)
            else:
                self._send_json({"error": "未知接口"}, 404)
        except Exception as e:
            logger.exception("API处理失败: %s", parsed.path)
            self._send_json({"error": str(e)}, 500)

    def handle_api_post(self, parsed: urlparse):
        """分发 /api/* POST 请求（策略保存/更新/删除）"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = {}
            if length > 0:
                body = json.loads(self.rfile.read(length).decode("utf-8")) or {}
            if parsed.path == "/api/strategy/save":
                self.api_strategy_save(body)
            elif parsed.path == "/api/strategy/update":
                self.api_strategy_update(body)
            elif parsed.path == "/api/strategy/delete":
                self.api_strategy_delete(body)
            elif parsed.path == "/api/backtest/result":
                self.api_backtest_result_save(body)
            elif parsed.path == "/api/predict/save":
                self.api_predict_save(body)
            elif parsed.path == "/api/predictions/import":
                self.api_predictions_import(body)
            else:
                self._send_json({"error": "未知接口"}, 404)
        except Exception as e:
            logger.exception("API处理失败: %s", parsed.path)
            self._send_json({"error": str(e)}, 500)

    def _parse_strategy_params(self, params):
        """兼容 DB 返回的 JSONB 字段：psycopg2 对 jsonb 默认返回 str，统一转成 dict"""
        import json as _json
        if isinstance(params, str):
            try:
                return _json.loads(params)
            except (ValueError, TypeError):
                return {}
        return params or {}

    def api_strategies(self, params: dict):
        """返回已保存的策略列表（策略表）"""
        from database.db import get_strategies

        strategies = get_strategies()
        data = []
        for s in strategies:
            row = {
                "id": s["id"],
                "strategy_name": s["strategy_name"],
                "strategy_type": s["strategy_type"],
                "params": self._parse_strategy_params(s["params"]),
                "description": s.get("description") or "",
                "is_active": s["is_active"],
                "created_at": s["created_at"].strftime("%Y-%m-%d %H:%M") if s.get("created_at") else "",
                "updated_at": s["updated_at"].strftime("%Y-%m-%d %H:%M") if s.get("updated_at") else "",
            }
            data.append(row)
        self._send_json({"data": data}, 200)

    def api_backtest_results(self, params: dict):
        """返回回测运行结果列表，可选 ?strategy_id=X 过滤"""
        from database.db import get_backtest_results

        strategy_id = None
        if params.get("strategy_id"):
            try:
                strategy_id = int(params["strategy_id"])
            except (TypeError, ValueError):
                self._send_json({"error": "无效参数 strategy_id"}, 400)
                return
        limit = 50
        if params.get("limit"):
            try:
                limit = min(int(params["limit"]), 200)
            except (TypeError, ValueError):
                pass
        results = get_backtest_results(limit=limit, strategy_id=strategy_id)
        data = []
        for r in results:
            row = {
                "id": r["id"],
                "strategy_id": r["strategy_id"],
                "strategy_name": r["strategy_name"],
                "symbol": r["symbol"],
                "start_date": r["start_date"].strftime("%Y-%m-%d") if r.get("start_date") else "",
                "end_date": r["end_date"].strftime("%Y-%m-%d") if r.get("end_date") else "",
                "initial_capital": float(r["initial_capital"] or 0),
                "final_capital": float(r["final_capital"] or 0),
                "total_return": float(r["total_return"] or 0),
                "annual_return": float(r["annual_return"] or 0),
                "max_drawdown": float(r["max_drawdown"] or 0),
                "sharpe_ratio": float(r["sharpe_ratio"] or 0),
                "win_rate": float(r["win_rate"] or 0),
                "total_trades": r["total_trades"] or 0,
                "trading_days": r["trading_days"] or 0,
                "total_commission": float(r["total_commission"] or 0),
                "created_at": r["created_at"].strftime("%Y-%m-%d %H:%M:%S") if r.get("created_at") else "",
            }
            data.append(row)
        self._send_json({"data": data}, 200)

    def api_backtest_result_save(self, body: dict):
        """保存回测结果：{strategy_id?, strategy_name, symbol, start_date, end_date,
           initial_capital, final_capital, total_return, annual_return, max_drawdown,
           sharpe_ratio, win_rate?, total_trades?, trading_days?, total_commission?, params?}"""
        from database.db import save_backtest_result

        required = ["symbol", "start_date", "end_date"]
        for key in required:
            if not body.get(key):
                self._send_json({"error": f"缺少参数 {key}"}, 400)
                return
        strategy_id = body.get("strategy_id")
        try:
            strategy_id = int(strategy_id) if strategy_id else None
        except (TypeError, ValueError):
            strategy_id = None
        result_id = save_backtest_result(
            strategy_name=(body.get("strategy_name") or "未命名策略").strip(),
            symbol=str(body["symbol"]).strip(),
            start_date=str(body["start_date"]),
            end_date=str(body["end_date"]),
            initial_capital=float(body.get("initial_capital") or 0),
            final_capital=float(body.get("final_capital") or 0),
            total_return=float(body.get("total_return") or 0),
            annual_return=float(body.get("annual_return") or 0),
            max_drawdown=float(body.get("max_drawdown") or 0),
            sharpe_ratio=float(body.get("sharpe_ratio") or 0),
            win_rate=float(body.get("win_rate") or 0),
            total_trades=int(body.get("total_trades") or 0),
            trading_days=int(body.get("trading_days") or 0),
            total_commission=float(body.get("total_commission") or 0),
            strategy_id=strategy_id,
            params=body.get("params") or {},
        )
        self._send_json({"ok": True, "id": result_id, "message": "回测结果已保存"}, 200)

    def api_predict_save(self, body: dict):
        """保存买卖点预测结论

        请求: {symbol, name?, based_date, predict_date, action, action_code,
               score?, prob_up?, close?, support_price?, support2_price?,
               resistance_price?, resistance2_price?, stop_loss?, factors?, weights?}
        同一股票同一 based_date 重复保存会覆盖更新。
        """
        from database.db import save_prediction

        required = ["symbol", "based_date", "predict_date", "action", "action_code"]
        for key in required:
            if not body.get(key):
                self._send_json({"error": f"缺少参数 {key}"}, 400)
                return
        try:
            pred_id = save_prediction(
                symbol=str(body["symbol"]).strip(),
                name=(body.get("name") or "").strip() or None,
                based_date=str(body["based_date"]),
                predict_date=str(body["predict_date"]),
                action=str(body["action"]).strip(),
                action_code=str(body["action_code"]).strip(),
                score=_opt_float(body.get("score")),
                prob_up=_opt_float(body.get("prob_up")),
                close=_opt_float(body.get("close")),
                support_price=_opt_float(body.get("support_price")),
                support2_price=_opt_float(body.get("support2_price")),
                resistance_price=_opt_float(body.get("resistance_price")),
                resistance2_price=_opt_float(body.get("resistance2_price")),
                stop_loss=_opt_float(body.get("stop_loss")),
                factors=body.get("factors") or [],
                weights=body.get("weights") or {},
                trend=(body.get("trend") or "").strip() or None,
                trend_probs=body.get("trend_probs") or {},
                trend_strategy=body.get("trend_strategy") or None,
            )
        except Exception as e:
            logger.exception("保存预测结论失败: %s", e)
            self._send_json({"error": f"保存失败: {e}"}, 500)
            return
        self._send_json({"ok": True, "id": pred_id, "message": "预测结论已保存，次日自动复盘"}, 200)

    def api_predictions_import(self, body: dict):
        """批量导入回测信号为预测记录（补充复盘样本）

        回测页把历史信号批量写入 predictions 表，随后 get_predictions/get_prediction_stats
        会自动对每条记录复盘（K线已在库中），快速积累命中/未命中样本供策略迭代。
        同一股票同一 based_date 重复导入会覆盖（幂等）。
        参数: {records: [{symbol,name,based_date,predict_date,action,action_code,score,
                          prob_up,close,support_price,support2_price,resistance_price,
                          resistance2_price,stop_loss,factors,weights,trend,...}]}
        """
        from database.db import save_prediction

        records = body.get("records") or []
        if not isinstance(records, list) or not records:
            self._send_json({"error": "缺少 records 列表"}, 400)
            return
        imported = 0
        errors = []
        for i, rec in enumerate(records):
            try:
                required = ["symbol", "based_date", "predict_date", "action", "action_code"]
                missing = [k for k in required if not rec.get(k)]
                if missing:
                    raise ValueError(f"缺少必填字段 {missing}")
                save_prediction(
                    symbol=str(rec["symbol"]).strip(),
                    name=(rec.get("name") or "").strip() or None,
                    based_date=str(rec["based_date"]),
                    predict_date=str(rec["predict_date"]),
                    action=str(rec["action"]).strip(),
                    action_code=str(rec["action_code"]).strip(),
                    score=_opt_float(rec.get("score")),
                    prob_up=_opt_float(rec.get("prob_up")),
                    close=_opt_float(rec.get("close")),
                    support_price=_opt_float(rec.get("support_price")),
                    support2_price=_opt_float(rec.get("support2_price")),
                    resistance_price=_opt_float(rec.get("resistance_price")),
                    resistance2_price=_opt_float(rec.get("resistance2_price")),
                    stop_loss=_opt_float(rec.get("stop_loss")),
                    factors=rec.get("factors") or [],
                    weights=rec.get("weights") or {},
                    trend=(rec.get("trend") or "").strip() or None,
                    trend_probs=rec.get("trend_probs") or {},
                    trend_strategy=rec.get("trend_strategy") or None,
                )
                imported += 1
            except Exception as e:
                errors.append({"index": i, "symbol": rec.get("symbol"), "error": str(e)})
        self._send_json({"ok": True, "imported": imported, "errors": errors[:20]}, 200)

    def _sync_pending_predictions(self):
        """对存在未复盘预测的股票做增量行情同步，保证次日复盘能取到实际收盘价。

        复盘依赖"预测日之后的首个交易日"行情：若该股K线未同步到最新，复盘会一直
        停留在"待复盘"。这里在读取预测记录前先补齐未复盘股票的尾部缺口（增量，
        只拉缺失日期），随后 get_predictions 内部的自动复盘即可生效。
        """
        try:
            from database.db import get_pending_prediction_symbols

            symbols = get_pending_prediction_symbols()
            if not symbols:
                return
            from data.fetcher import DataFetcher
            from datetime import datetime

            fetcher = DataFetcher()
            today = datetime.now().strftime("%Y%m%d")
            for sym in symbols:
                try:
                    fetcher.fetch_stock(sym, "19900101", today, use_db=True)
                except Exception as e:
                    logger.warning("同步 %s 行情失败（复盘暂缓）: %s", sym, e)
        except Exception as e:
            logger.warning("同步未复盘预测行情失败: %s", e)

    def api_predictions(self, params: dict):
        """获取预测记录列表（自动复盘未复盘记录）

        参数: symbol=股票代码(可选), limit=条数(默认100)
        返回: {data: [{id,symbol,name,based_date,predict_date,action,action_code,score,
                       prob_up,close,support_price,resistance_price,stop_loss,
                       review_date,actual_close,actual_pct,hit,factor...}]}
        """
        from database.db import get_predictions

        self._sync_pending_predictions()
        symbol = (params.get("symbol") or "").strip() or None
        limit = 100
        if params.get("limit"):
            try:
                limit = min(int(params["limit"]), 500)
            except (TypeError, ValueError):
                pass
        data = get_predictions(symbol=symbol, limit=limit)
        self._send_json({"data": data}, 200)

    def api_predictions_stats(self, params: dict):
        """预测记录汇总统计（自动复盘后计算命中率/因子有效性/策略建议）"""
        from database.db import get_prediction_stats

        self._sync_pending_predictions()
        self._send_json(get_prediction_stats(), 200)

    def api_strategy_save(self, body: dict):
        """保存策略：{strategy_name, strategy_type, params, description?} → 返回 {id}"""
        from database.db import save_strategy

        name = (body.get("strategy_name") or "").strip()
        stype = (body.get("strategy_type") or "").strip()
        params = body.get("params") or {}
        description = (body.get("description") or "").strip()
        if not name:
            self._send_json({"error": "缺少参数 strategy_name"}, 400)
            return
        if not stype:
            self._send_json({"error": "缺少参数 strategy_type"}, 400)
            return
        strategy_id = save_strategy(name, stype, params, description=description or None)
        self._send_json({"ok": True, "id": strategy_id, "message": f"策略「{name}」已保存"}, 200)

    def api_strategy_update(self, body: dict):
        """更新策略：{id, strategy_name?, params?, description?}"""
        from database.db import update_strategy

        try:
            strategy_id = int(body.get("id"))
        except (TypeError, ValueError):
            self._send_json({"error": "缺少或无效参数 id"}, 400)
            return
        name = (body.get("strategy_name") or "").strip()
        params = body.get("params")
        description = body.get("description")
        update_strategy(
            strategy_id,
            strategy_name=name or None,
            params=params if params is not None else None,
            description=description.strip() if isinstance(description, str) and description.strip() else None,
        )
        self._send_json({"ok": True, "message": "策略已更新"}, 200)

    def api_strategy_delete(self, body: dict):
        """删除策略：{id}"""
        from database.db import delete_strategy

        try:
            strategy_id = int(body.get("id"))
        except (TypeError, ValueError):
            self._send_json({"error": "缺少或无效参数 id"}, 400)
            return
        delete_strategy(strategy_id)
        self._send_json({"ok": True, "message": "策略已删除"}, 200)

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

    def api_kline_db(self, params: dict):
        """仅从本地数据库读取日K线数据（不联网拉取）

        参数: code=股票代码, beg=开始日期YYYYMMDD/YYYY-MM-DD, end=结束日期（缺省为全部）
        返回: { data: [{date,open,close,high,low,volume,amount,turnover,...}], stock: {code,name} }
        """
        code = (params.get("code") or "").strip()
        if not code:
            self._send_json({"error": "缺少参数 code"}, 400)
            return

        from database.db import get_kline

        beg = (params.get("beg") or "").strip() or "1900-01-01"
        end = (params.get("end") or "").strip() or "2100-01-01"
        logger.info("API /api/kline_db: code=%s beg=%s end=%s", code, beg, end)
        df = get_kline(code, beg, end)
        if df.empty:
            self._send_json({"error": f"库中暂无 {code} 的K线数据"}, 404)
            return

        records = df.to_dict(orient="records")
        for r in records:
            r["date"] = r["date"].strftime("%Y-%m-%d")
        self._send_json(
            {"data": records, "stock": {"code": code, "name": _stock_name(code)}},
            200,
        )

    def api_index_kline(self, params: dict):
        """返回大盘指数日K线（默认沪深300 sh.000300，供买卖点预测的大盘情绪/波动率过滤使用）

        库中无该指数或尾部缺口（最新数据距今>4天）时，自动用 baostock 增量拉取入库；
        拉取失败（沙箱网络不可用等）时返回库内已有数据并提示。
        """
        import datetime as _dt
        import sys as _sys

        from database.db import get_kline, get_kline_max_date

        code = (params.get("code") or "sh.000300").strip()
        name = (params.get("name") or "沪深300指数").strip()

        max_dt = get_kline_max_date(code)
        today = _dt.date.today()
        need_fetch = False
        if not max_dt:
            need_fetch = True
        else:
            try:
                last = _dt.date.fromisoformat(max_dt)
                need_fetch = (today - last).days > 4
            except ValueError:
                need_fetch = True

        if need_fetch:
            try:
                _sys.path.insert(0, str(ROOT / "scripts"))
                from fetch_index import _login_baostock, fetch_index

                bs = _login_baostock()
                if bs:
                    try:
                        fetch_index(bs, code, name, "")
                    finally:
                        try:
                            bs.logout()
                        except Exception:
                            pass
            except Exception as e:
                logger.warning("指数增量拉取失败，返回库内已有数据: %s", e)

        df = get_kline(code, "1900-01-01", "2100-01-01")
        if df.empty:
            self._send_json(
                {"error": f"库中暂无 {code} 指数K线，可用本机执行 scripts\\fetch_index.py 拉取"},
                404,
            )
            return
        records = df.to_dict(orient="records")
        for r in records:
            r["date"] = r["date"].strftime("%Y-%m-%d")
        self._send_json({"data": records, "index": {"code": code, "name": name}}, 200)

    def api_stock_list(self, params: dict):
        """返回库中已有K线数据的股票列表（供下拉选择）"""
        from database.db import db_cursor

        with db_cursor() as cur:
            cur.execute(
                """SELECT DISTINCT k.symbol, COALESCE(s.name, '') AS name
                   FROM market_data.daily_kline k
                   LEFT JOIN market_data.stock_info s ON s.symbol = k.symbol
                   ORDER BY k.symbol"""
            )
            rows = cur.fetchall()

        stocks = [{"Code": r[0], "Name": r[1]} for r in rows]
        self._send_json({"data": stocks}, 200)

    def api_stock_catalog(self, params: dict):
        """返回 stock_info 表全部A股（代码+名称），供「下拉+输入」联想使用

        可选参数: keyword=模糊过滤（代码或名称，可空则返回全部，限量5000）
        """
        keyword = (params.get("keyword") or "").strip()
        from database.db import db_cursor

        sql = """SELECT symbol, COALESCE(name, '') AS name, market
                 FROM market_data.stock_info"""
        args: list = []
        if keyword:
            sql += " WHERE symbol LIKE %s OR name LIKE %s"
            args = [f"%{keyword}%", f"%{keyword}%"]
        sql += " ORDER BY symbol LIMIT 5000"

        with db_cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()

        stocks = [
            {"Code": r[0], "Name": r[1], "Market": r[2]}
            for r in rows
        ]
        self._send_json({"data": stocks}, 200)

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
        """查询股票基本信息（含上市日期、股本、市值，来自库表 stock_info）

        参数: code=股票代码
        返回: {symbol,name,market,list_date,industry,total_share,float_share,
               total_market_cap,float_market_cap}，list_date 为 'YYYY-MM-DD' 或 null
        """
        code = (params.get("code") or "").strip()
        if not code:
            self._send_json({"error": "缺少参数 code"}, 400)
            return

        from database.db import db_cursor

        with db_cursor() as cur:
            cur.execute(
                """SELECT symbol, name, market, list_date, industry,
                          total_share, float_share, total_market_cap, float_market_cap
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
            "total_share": float(row[5]) if row[5] else None,
            "float_share": float(row[6]) if row[6] else None,
            "total_market_cap": float(row[7]) if row[7] else None,
            "float_market_cap": float(row[8]) if row[8] else None,
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

        # 数据预览分页参数（page 从 1 开始，每页 page_size 条）
        try:
            page = max(1, int(params.get("page") or 1))
        except (TypeError, ValueError):
            page = 1
        page_size = 20

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
            # 统一返回统计与预览（范围取库中全部记录，分页返回）
            full = get_kline(code, "1900-01-01", "2100-01-01")
            preview, page, total_pages = self._slice_page(full, page, page_size)
            self._send_json(
                {"ok": True, "source": "sina", "code": code, "name": _stock_name(code),
                 "list_date": get_stock_list_date(code),
                 "count": len(full),
                 "first": full["date"].min().strftime("%Y-%m-%d"),
                 "last": full["date"].max().strftime("%Y-%m-%d"),
                 "last_close": float(full.iloc[-1]["close"]) if not full.empty else None,
                 "saved": saved, "preview": preview,
                 "page": page, "page_size": page_size, "total_pages": total_pages,
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

        # 从库返回统计与预览（分页返回）
        full = get_kline(code, beg, end)
        if full.empty:
            self._send_json({"error": f"入库后仍未获取到 {code} 的K线数据"}, 404)
            return
        preview, page, total_pages = self._slice_page(full, page, page_size)
        self._send_json(
            {"ok": True, "source": source, "code": code, "name": _stock_name(code),
             "list_date": get_stock_list_date(code),
             "count": len(full),
             "first": full["date"].min().strftime("%Y-%m-%d"),
             "last": full["date"].max().strftime("%Y-%m-%d"),
             "last_close": float(full.iloc[-1]["close"]),
             "saved": saved, "preview": preview,
             "page": page, "page_size": page_size, "total_pages": total_pages},
            200,
        )

    def api_refresh_today(self, params: dict):
        """强制刷新库中股票的当天数据（重拉当天日K并覆盖入库）

        背景：增量拉取在库中已有当天数据时不再重拉，盘中拉取的快照不会随行情
        更新。此接口绕过增量逻辑，强制重拉当天并覆盖库中当天行。

        参数: code=股票代码(可选，省略时刷新库中所有股票), source=数据源(默认auto)
        返回: {ok, code, name, source, before, after, changed, message}
        """
        import pandas as pd

        code = (params.get("code") or "").strip()
        source = (params.get("source") or "auto").strip() or "auto"
        if not code:
            self.api_refresh_today_all(source)
            return
        if source in ("sina", "tdx", "akshare"):
            source = "auto"  # 资金流/分钟K线/akshare 不适用，回退自动

        from database.db import get_kline
        from data.fetcher import DataFetcher

        # 刷新前：库中今天的行
        today_str = pd.Timestamp.today().normalize().strftime("%Y-%m-%d")
        before_df = get_kline(code, today_str, today_str)
        before = before_df.to_dict(orient="records")
        for r in before:
            r["date"] = r["date"].strftime("%Y-%m-%d")

        try:
            df = DataFetcher().refresh_today(code, source)
        except Exception as e:
            logger.exception("刷新当天 %s 失败: %s", code, e)
            self._send_json({"error": f"刷新失败: {e}"}, 500)
            return
        if df.empty:
            self._send_json(
                {"error": f"{code} 今天（{today_str}）非交易日或数据源暂无当天数据，未刷新"},
                404,
            )
            return

        after = df.to_dict(orient="records")
        for r in after:
            r["date"] = r["date"].strftime("%Y-%m-%d")
        used = str(df["used_source"].iloc[0]) if "used_source" in df.columns else source

        # 前后对比（NaN 视为相同，不列为变化）
        fields = ["open", "high", "low", "close", "volume", "amount",
                  "turnover", "amplitude", "pct_change"]
        changed = {}
        if before:
            for f in fields:
                if not _same_value(before[0].get(f), after[0].get(f)):
                    changed[f] = {"before": before[0].get(f), "after": after[0].get(f)}

        diff_parts = []
        if before and before[0].get("close") is not None and after[0].get("close") is not None:
            diff_parts.append(f"收盘 {before[0]['close']:.2f} → {after[0]['close']:.2f}")
        if before and before[0].get("amplitude") is not None and after[0].get("amplitude") is not None:
            diff_parts.append(f"振幅 {before[0]['amplitude']:.2f}% → {after[0]['amplitude']:.2f}%")
        message = f"已用 {used} 源刷新 {code} 当天数据（{after[0]['date']}）"
        if diff_parts:
            message += "：" + "，".join(diff_parts)
        elif before:
            message += "：与库中原有数据一致"

        self._send_json(
            {"ok": True, "code": code, "name": _stock_name(code),
             "source": used, "before": before[0] if before else None,
             "after": after[0], "changed": changed, "message": message},
            200,
        )

    def api_probe(self, params: dict):
        """字段缺失探针：扫描指定股票日线/股票信息缺失字段，可补全

        参数:
          code=股票代码（必填）
          fields=逗号分隔字段（默认全部）
            turnover / amount / volume_ratio / fund_flow / stock_info
          fix=0 仅扫描本地库（不联网不写库）；fix=1 或省略则扫描并自动补全
        返回: {symbol, fields: {字段: {status, missing, filled, range, reason, fix}}}
        """
        code = (params.get("code") or "").strip()
        if not code:
            self._send_json({"error": "缺少参数 code"}, 400)
            return
        fields = [f.strip() for f in (params.get("fields") or "").split(",") if f.strip()] or None
        fix = params.get("fix") in ("1", "true", "True", "yes", "on")
        from data.fetcher import DataFetcher

        try:
            report = DataFetcher().probe_and_fix(code, fields, fix=fix)
        except Exception as e:
            logger.exception("探针 %s 失败: %s", code, e)
            self._send_json({"error": f"探针执行失败: {e}"}, 500)
            return
        self._send_json(report, 200)

    def api_backfill_status(self, params: dict):
        """补齐任务监控状态（读取补齐脚本落盘文件，实时返回进度/失败清单）

        读取 _backfill_progress.json（进度）、_backfill_failed.json（失败清单）、
        _backfill_kline.log（OK/FAIL/重试行），统计 OK 行时间戳估算速率。
        返回: {running, goal, done, ok, failed, skipped, started,
              sec_per, eta_seconds, failed_list:[{symbol,name,msg,time}], last_result, retry_lines}
        """
        import re as _re
        from datetime import datetime as _dt

        ROOT_DIR = ROOT
        prog_file = ROOT_DIR / "_backfill_progress.json"
        fail_file = ROOT_DIR / "_backfill_failed.json"
        log_file = ROOT_DIR / "_backfill_kline.log"

        def _load_json(path, default):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f) or default
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return default

        progress = _load_json(prog_file, {})
        failed = _load_json(fail_file, [])

        # 库中已有 K 线股票数（权威实时）
        try:
            from database.db import db_cursor

            with db_cursor() as cur:
                cur.execute("SELECT COUNT(DISTINCT symbol) FROM market_data.daily_kline")
                done = cur.fetchone()[0]
        except Exception:
            done = progress.get("ok", 0)
        try:
            from database.db import db_cursor

            with db_cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM market_data.stock_info")
                goal = cur.fetchone()[0]
        except Exception:
            goal = progress.get("total", 0) + done

        # 日志统计：OK/FAIL 行数、时间戳、最近结果、重试行
        log_ok = log_fail = 0
        last_result = ""
        retry_lines = []
        stamps = []
        now = _dt.now()
        today = now.date()
        ok_re = _re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s*OK\s+")
        fail_re = _re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s*FAIL\s+")
        retry_re = _re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s*重试\s+(\S+)")
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    ln = ln.rstrip("\n")
                    m = ok_re.match(ln)
                    if m:
                        log_ok += 1
                        last_result = ln
                        try:
                            t = _dt.strptime(m.group(1), "%H:%M:%S").replace(
                                year=today.year, month=today.month, day=today.day
                            )
                            if t > now:
                                t = t.replace(year=today.year - 1)
                            stamps.append(t.timestamp())
                        except ValueError:
                            pass
                        continue
                    m = fail_re.match(ln)
                    if m:
                        log_fail += 1
                        last_result = ln
                        continue
                    if retry_re.match(ln):
                        retry_lines.append(ln)
        except FileNotFoundError:
            pass

        # 速率：最近 20 条 OK 的平均间隔（秒/只）
        sec_per = 0.0
        if len(stamps) >= 2:
            win = stamps[-20:]
            sec_per = (win[-1] - win[0]) / (len(win) - 1) if len(win) > 1 else 0.0
        remaining = max(goal - done, 0)
        eta_seconds = remaining * sec_per if sec_per > 0 and remaining > 0 else 0
        # 活跃判定：最后一条 OK/FAIL 距今 < 10 分钟 或 进度文件 failed>0 待处理
        running = bool(retry_lines) or (log_ok + log_fail) > 0 and _dt.now().timestamp() - (stamps[-1] if stamps else 0) < 600

        self._send_json({
            "running": bool(running),
            "goal": goal,
            "done": done,
            "remaining": remaining,
            "ok": progress.get("ok", 0),
            "failed": progress.get("failed", 0),
            "skipped": progress.get("skipped", 0),
            "started": progress.get("started", ""),
            "log_ok": log_ok,
            "log_fail": log_fail,
            "sec_per": round(sec_per, 1) if sec_per else 0,
            "eta_seconds": int(eta_seconds),
            "last_result": last_result,
            "retry_lines": retry_lines[-5:],
            "failed_list": failed,
        }, 200)

    def api_refresh_today_all(self, source: str):
        """批量刷新库中所有股票的当天数据（/api/refresh_today 未传 code 时调用）"""
        import pandas as pd

        from database.db import db_cursor, get_kline
        from data.fetcher import DataFetcher

        with db_cursor() as cur:
            cur.execute(
                """SELECT DISTINCT k.symbol
                   FROM market_data.daily_kline k
                   ORDER BY k.symbol"""
            )
            symbols = [r[0] for r in cur.fetchall()]
        if not symbols:
            self._send_json({"error": "日线库中暂无股票数据"}, 404)
            return

        today_str = pd.Timestamp.today().normalize().strftime("%Y-%m-%d")
        fields = ["open", "high", "low", "close", "volume", "amount",
                  "turnover", "amplitude", "pct_change"]
        fetcher = DataFetcher()
        items = []
        for sym in symbols:
            item = {"code": sym, "name": _stock_name(sym), "date": today_str}
            try:
                df = fetcher.refresh_today(sym, source)
                if df.empty:
                    item["error"] = "今天非交易日或数据源暂无当天数据"
                    items.append(item)
                    continue
                after = df.to_dict(orient="records")[0]
                after["date"] = after["date"].strftime("%Y-%m-%d")
                item["source"] = str(df["used_source"].iloc[0]) if "used_source" in df.columns else source
                item["after"] = after
                before_df = get_kline(sym, today_str, today_str)
                if not before_df.empty:
                    before = before_df.to_dict(orient="records")[0]
                    before["date"] = before["date"].strftime("%Y-%m-%d")
                    item["before"] = before
                    changed = {}
                    for f in fields:
                        if not _same_value(before.get(f), after.get(f)):
                            changed[f] = {"before": before.get(f), "after": after.get(f)}
                    item["changed"] = changed
            except Exception as e:
                logger.exception("批量刷新 %s 当天数据失败: %s", sym, e)
                item["error"] = f"刷新失败: {e}"
            items.append(item)

        ok_count = sum(1 for it in items if not it.get("error"))
        fail_count = len(items) - ok_count
        self._send_json(
            {"ok": True, "mode": "all", "total": len(items),
             "ok_count": ok_count, "fail_count": fail_count,
             "items": items,
             "message": f"共刷新 {len(items)} 只：成功 {ok_count} 只，失败 {fail_count} 只"},
            200,
        )

    # ---------- 工具 ----------

    def _slice_page(self, df, page: int, page_size: int):
        """把完整数据框按页切片，返回 (本页记录, 实际页码, 总页数)"""
        total = len(df)
        total_pages = max(1, math.ceil(total / page_size))
        page = min(max(1, page), total_pages)
        start = (page - 1) * page_size
        rows = df.iloc[start:start + page_size].to_dict(orient="records")
        for r in rows:
            r["date"] = r["date"].strftime("%Y-%m-%d")
        return rows, page, total_pages

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
        body = json.dumps(_sanitize(obj), ensure_ascii=False, default=_json_default).encode("utf-8")
        try:
            self.send_response(status)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as e:
            # 客户端提前断开（如全量拉取耗时较长时刷新/关闭页面）：业务数据已入库，忽略响应写失败
            logger.warning("客户端断开，响应未送达 %s: %s", self.path, e)

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)


def main():
    parser = argparse.ArgumentParser(description="本地量化回测服务")
    parser.add_argument("--port", type=int, default=8000, help="服务端口，默认 8000")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), QuantHandler)
    print(f"量化工具服务已启动: http://127.0.0.1:{args.port}/")
    print(f"功能入口: /（index.html）, /kline.html, /backtest.html, /macd.html")
    print(f"数据API: /api/kline, /api/search （浏览器拉数据时自动入库）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()
