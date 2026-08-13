# -*- coding: utf-8 -*-
"""单元测试：DataFetcher._infer_market_code 交易所代码推断

回归防护：2026-08-13 修复北交所判定 —— 920/8/4 开头代码曾被误判为沪市(sh)，
导致 baostock 回退链失败（返回空数据）。此测试防止未来回归。
"""
import unittest

from data.fetcher import DataFetcher


class TestInferMarketCode(unittest.TestCase):
    def setUp(self):
        self.fetcher = DataFetcher()

    def test_beijing_920(self):
        """北交所新代码段 920 开头 -> bj（本次修复的核心）"""
        for code in ["920010", "920021", "920185", "920964"]:
            with self.subTest(code=code):
                self.assertEqual(self.fetcher._infer_market_code(code), "bj")

    def test_beijing_8(self):
        """北交所 8 开头 -> bj"""
        for code in ["830799", "831010", "832000", "870000"]:
            with self.subTest(code=code):
                self.assertEqual(self.fetcher._infer_market_code(code), "bj")

    def test_beijing_4(self):
        """北交所 4 开头 -> bj"""
        for code in ["430000", "430017", "430047"]:
            with self.subTest(code=code):
                self.assertEqual(self.fetcher._infer_market_code(code), "bj")

    def test_shanghai_6(self):
        """沪市 6 开头 -> sh"""
        for code in ["600000", "600636", "601021", "603118"]:
            with self.subTest(code=code):
                self.assertEqual(self.fetcher._infer_market_code(code), "sh")

    def test_shanghai_9(self):
        """沪市 B 股 9 开头 -> sh（920 已先行匹配 bj，剩余 9 开头为沪市）"""
        for code in ["900901", "900902", "900903"]:
            with self.subTest(code=code):
                self.assertEqual(self.fetcher._infer_market_code(code), "sh")

    def test_shenzhen_0(self):
        """深市 0 开头 -> sz"""
        for code in ["000001", "000636", "002407", "000708"]:
            with self.subTest(code=code):
                self.assertEqual(self.fetcher._infer_market_code(code), "sz")

    def test_shenzhen_3(self):
        """深市创业板 3 开头 -> sz"""
        for code in ["300058", "300413", "300418", "301000"]:
            with self.subTest(code=code):
                self.assertEqual(self.fetcher._infer_market_code(code), "sz")


if __name__ == "__main__":
    unittest.main()
