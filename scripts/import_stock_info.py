"""导入 A 股股票基础信息到 market_data.stock_info

数据源：akshare stock_info_a_code_name()（新浪源，稳定）
用法：
  python scripts/import_stock_info.py                       # 仅基础信息（代码+名称+股市）
  python scripts/import_stock_info.py --detail              # 额外获取详细信息（行业/市值）
  python scripts/import_stock_info.py --concept             # 额外获取概念板块
  python scripts/import_stock_info.py --detail --concept    # 全量
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import akshare as ak
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

from database.db import get_connection


def infer_market(code: str) -> str:
    """根据代码前缀推断所属股市"""
    if code.startswith("60"):
        return "沪市主板"
    elif code.startswith("68"):
        return "科创板"
    elif code.startswith("00"):
        return "深市主板"
    elif code.startswith("30"):
        return "创业板"
    elif code.startswith("8") or code.startswith("4") or code.startswith("920"):
        return "北交所"
    else:
        return "其他"


def import_basic(cur) -> list[str]:
    """导入全部 A 股基础信息（代码+名称+所属股市），返回所有 symbol"""
    df = ak.stock_info_a_code_name()
    print(f"从 akshare 获取到 {len(df)} 只 A 股")

    rows = []
    for _, r in df.iterrows():
        code = str(r["code"]).zfill(6)
        name = str(r["name"]).strip()
        market = infer_market(code)
        rows.append((code, name, market))

    sql = """
        INSERT INTO market_data.stock_info (symbol, name, market)
        VALUES %s
        ON CONFLICT (symbol) DO UPDATE SET
            name = EXCLUDED.name, market = EXCLUDED.market,
            updated_at = CURRENT_TIMESTAMP
    """
    execute_values(cur, sql, rows)
    print(f"基础信息导入完成：{len(rows)} 条")
    return [r[0] for r in rows]


def import_detail_bulk(existing_conn):
    """批量获取详细信息（行业、市值等），使用新浪实时行情接口，一次拉取全部"""
    print("通过 stock_classify_sina() 批量获取行情与行业信息...")
    df = ak.stock_classify_sina()
    print(f"获取到 {len(df)} 条数据")

    # 过滤仅A股（symbol 以 sz 或 sh 开头）
    df_a = df[df["symbol"].str.match(r"^(sz|sh)\d{6}$")].copy()

    # 去重：按 code 保留最后一条（最新快照）
    df_a["code"] = df_a["code"].astype(str).str.zfill(6)
    df_a = df_a.drop_duplicates(subset=["code"], keep="last")
    print(f"去重后 A 股 {len(df_a)} 条")

    # mktcap/nmc 单位是"万元"，转为"亿"
    def to_yi(val):
        if val is None or pd.isna(val):
            return None
        try:
            return round(float(val) / 10000, 2)
        except (ValueError, TypeError):
            return None

    # 构建批量更新数据
    rows = []
    for _, row in df_a.iterrows():
        code = str(row["code"]).zfill(6)
        industry = row.get("class", None)
        if isinstance(industry, float) and pd.isna(industry):
            industry = None
        rows.append((
            industry,
            to_yi(row.get("mktcap")),
            to_yi(row.get("nmc")),
            code,
        ))

    # 用新连接批量写入（旧连接可能已超时断开）
    conn = get_connection()
    conn.autocommit = True
    # 设置 keepalive 防止长查询被断开
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    try:
        from psycopg2.extras import execute_values
        sql = """
            UPDATE market_data.stock_info AS t SET
                industry = v.industry,
                total_market_cap = v.total_market_cap,
                float_market_cap = v.float_market_cap,
                updated_at = CURRENT_TIMESTAMP
            FROM (VALUES %s) AS v(industry, total_market_cap, float_market_cap, symbol)
            WHERE t.symbol = v.symbol
        """
        execute_values(cur, sql, rows, page_size=200)
        print(f"批量详细信息更新完成：影响 {len(rows)} 条记录")
    finally:
        cur.close()
        conn.close()


def import_detail_individually(cur, symbols: list[str], max_retry: int = 2):
    """逐个获取详细信息（行业、市值、上市日期、总股本等），东方财富源"""
    success = 0
    failed = 0
    total = len(symbols)

    for i, symbol in enumerate(symbols):
        if (i + 1) % 100 == 0:
            print(f"  进度: {i+1}/{total} (成功{success}, 失败{failed})", flush=True)

        for attempt in range(max_retry):
            try:
                df = ak.stock_individual_info_em(symbol=symbol)
                info = dict(zip(df["item"], df["value"]))

                list_date = info.get("上市时间", None)
                total_share = info.get("总股本", None)
                float_share = info.get("流通股", None)
                total_market_cap = info.get("总市值", None)
                float_market_cap = info.get("流通市值", None)

                def to_float(v):
                    if v is None:
                        return None
                    try:
                        return float(str(v).replace(",", "").replace("亿", ""))
                    except (ValueError, TypeError):
                        return None

                def parse_date(v):
                    if v is None:
                        return None
                    s = str(v).strip()
                    if len(s) == 8 and s.isdigit():
                        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
                    return s[:10] if len(s) >= 10 else None

                cur.execute("""
                    UPDATE market_data.stock_info
                    SET list_date = %s,
                        total_share = %s, float_share = %s,
                        total_market_cap = COALESCE(market_data.stock_info.total_market_cap, %s),
                        float_market_cap = COALESCE(market_data.stock_info.float_market_cap, %s),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE symbol = %s
                """, (
                    parse_date(list_date),
                    to_float(total_share), to_float(float_share),
                    to_float(total_market_cap), to_float(float_market_cap),
                    symbol,
                ))
                success += 1
                break
            except Exception:
                if attempt < max_retry - 1:
                    time.sleep(1)
                else:
                    failed += 1

        time.sleep(0.3)

    print(f"详细信息获取完成：成功 {success}，失败 {failed}")


def import_concepts(cur):
    """获取概念板块成分股，填充 concept 字段"""
    try:
        df = ak.stock_board_concept_name_em()
        print(f"获取到 {len(df)} 个概念板块")
    except Exception as e:
        print(f"概念板块接口不可用: {type(e).__name__}，跳过概念导入")
        return

    concept_map: dict[str, list[str]] = {}
    for _, row in df.iterrows():
        concept_name = row["板块名称"]
        try:
            cons = ak.stock_board_concept_cons_em(symbol=concept_name)
            for code in cons["代码"]:
                code = str(code).zfill(6)
                if code not in concept_map:
                    concept_map[code] = []
                concept_map[code].append(concept_name)
            time.sleep(0.2)
        except Exception:
            continue

    updated = 0
    for symbol, concepts in concept_map.items():
        cur.execute(
            "UPDATE market_data.stock_info SET concept = %s WHERE symbol = %s",
            (concepts, symbol),
        )
        updated += 1
    print(f"概念信息更新完成：{updated} 只股票")


def show_stats(cur):
    """统计导入结果"""
    cur.execute("SELECT COUNT(*) FROM market_data.stock_info")
    total = cur.fetchone()[0]
    cur.execute("SELECT market, COUNT(*) FROM market_data.stock_info GROUP BY market ORDER BY COUNT(*) DESC")
    by_market = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM market_data.stock_info WHERE industry IS NOT NULL")
    has_industry = cur.fetchone()[0]

    print(f"\n===== 导入完成 =====")
    print(f"总计: {total} 只股票")
    print(f"有行业信息: {has_industry} 只")
    print("按股市分布:")
    for m, c in by_market:
        print(f"  {m}: {c} 只")


def main():
    parser = argparse.ArgumentParser(description="导入 A 股股票信息")
    parser.add_argument("--detail", action="store_true", help="批量获取详细信息（行业/市值，快速）")
    parser.add_argument("--detail-individual", action="store_true", help="逐个获取详细信息（含上市日期/总股本，慢）")
    parser.add_argument("--concept", action="store_true", help="额外获取概念板块")
    args = parser.parse_args()

    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()

    try:
        symbols = import_basic(cur)

        if args.detail:
            import_detail_bulk(conn)

        if args.detail_individual:
            print(f"\n开始逐个获取详细信息（{len(symbols)} 只）...")
            import_detail_individually(cur, symbols)

        if args.concept:
            print("\n开始获取概念板块...")
            import_concepts(cur)

        show_stats(cur)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
