# -*- coding: utf-8 -*-
"""api/futures_bp 单元测试。

用 Flask test_client 直接测路由层，并 monkeypatch core.futures_service 的对外
函数返回内存假数据（不触达外部扩展行情服务器）。覆盖：
  - /futures/snapshot 快照（含涨跌/涨跌幅计算）；
  - /futures/exquote/{minute,quote,tick}/<code> 三个行情接口；
  - 轻量缓存：ttl 内第二次请求不再回查数据层；
  - ExHq 不可用时的兜底 JSON（ok=False）。
"""
import json
import unittest
from unittest import mock

from flask import Flask

import core.futures_service as fut
from api.futures_bp import futures_bp
import api.futures_bp as futures_bp_mod


def _canned_quote():
    return {
        "price": 4555.4,
        "last_close": 4556.4,
        "open": 4556.0,
        "high": 4574.4,
        "low": 4549.2,
        "chicang": 253128.0,
        "zongliang": 47847.0,
        "servertime": "12:57:56",
        "bids": [[4554.0, 2.0]] * 5,
        "asks": [[4557.0, 3.0]] * 5,
    }


def _canned_bars():
    return [
        {"date": "2026-08-28 09:31", "open": 4555.0, "high": 4556.0, "low": 4554.0,
         "close": 4555.5, "vol": 120, "position": 253000},
        {"date": "2026-08-28 09:32", "open": 4555.5, "high": 4557.0, "low": 4555.0,
         "close": 4556.2, "vol": 90, "position": 253100},
    ]


class FuturesBlueprintTest(unittest.TestCase):

    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(futures_bp, url_prefix="/futures")
        self.client = self.app.test_client()

        self._orig_exhq_ok = fut._EXHQ_OK
        fut._EXHQ_OK = True
        futures_bp_mod._EXQ_CACHE.clear()

        self.p_minute = mock.patch.object(fut, "fut_minute", return_value=[
            {"time": "09:31", "price": 4556.0, "avg_price": 4555.0, "vol": 2439}
        ]).start()
        self.p_quote = mock.patch.object(fut, "fut_quote", return_value=_canned_quote()).start()
        self.p_tick = mock.patch.object(fut, "fut_tick", return_value=[
            {"time": "11:30:01", "price": 4555.6, "vol": 2, "num": 1, "buyorsell": 0}
        ]).start()
        self.p_kline = mock.patch.object(fut, "fut_bars", return_value=_canned_bars()).start()

    def tearDown(self):
        mock.patch.stopall()
        futures_bp_mod._EXQ_CACHE.clear()
        fut._EXHQ_OK = self._orig_exhq_ok

    # ---- snapshot ----
    def test_snapshot_returns_four_contracts(self):
        resp = self.client.get("/futures/snapshot")
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.get_data(as_text=True))
        self.assertTrue(body["ok"])
        data = body["data"]
        self.assertEqual(len(data), 4)
        codes = {d["code"] for d in data}
        self.assertEqual(codes, {"IFL9", "IHL9", "ICL9", "IML9"})
        # 涨跌与涨跌幅由快照路由基于盘口价计算
        ifl9 = next(d for d in data if d["code"] == "IFL9")
        self.assertAlmostEqual(ifl9["change"], 4555.4 - 4556.4, places=6)
        self.assertAlmostEqual(ifl9["change_pct"], (4555.4 - 4556.4) / 4556.4 * 100, places=6)
        self.assertEqual(ifl9["chicang"], 253128.0)
        self.assertEqual(ifl9["zongliang"], 47847.0)

    # ---- exquote 路由 ----
    def test_minute_route(self):
        resp = self.client.get("/futures/exquote/minute/IFL9")
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.get_data(as_text=True))
        self.assertTrue(body["ok"])
        self.assertEqual(body["src"], "exhq")
        self.assertEqual(len(body["data"]), 1)
        self.assertEqual(body["data"][0]["time"], "09:31")

    def test_quote_route(self):
        resp = self.client.get("/futures/exquote/quote/IFL9")
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.get_data(as_text=True))
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["price"], 4555.4)
        self.assertEqual(len(body["data"]["bids"]), 5)

    def test_tick_route_with_count(self):
        resp = self.client.get("/futures/exquote/tick/IFL9?count=10")
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.get_data(as_text=True))
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["data"]), 1)
        # count 参数应透传到数据层
        self.p_tick.assert_called_with("IFL9", count=10)

    # ---- 缓存 ----
    def test_minute_route_uses_cache_within_ttl(self):
        self.client.get("/futures/exquote/minute/IFL9?ttl=30")
        self.client.get("/futures/exquote/minute/IFL9?ttl=30")
        # ttl 内第二次请求直接命中缓存，不回查数据层
        self.assertEqual(self.p_minute.call_count, 1)

    def test_minute_route_bypasses_cache_after_ttl(self):
        # ttl=0 视为不缓存，每次都回查
        self.client.get("/futures/exquote/minute/IFL9?ttl=0")
        self.client.get("/futures/exquote/minute/IFL9?ttl=0")
        self.assertEqual(self.p_minute.call_count, 2)

    # ---- kline 路由 ----
    def test_kline_route_passes_period_and_count(self):
        resp = self.client.get("/futures/exquote/kline/IFL9?period=5min&count=120")
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.get_data(as_text=True))
        self.assertTrue(body["ok"])
        self.assertEqual(body["src"], "exhq")
        self.assertEqual(len(body["data"]), 2)
        self.assertEqual(body["data"][0]["date"], "2026-08-28 09:31")
        # period + count 应透传到数据层
        self.p_kline.assert_called_with("IFL9", category="5min", count=120)

    def test_kline_route_defaults_to_1min_240(self):
        resp = self.client.get("/futures/exquote/kline/IFL9")
        body = json.loads(resp.get_data(as_text=True))
        self.assertTrue(body["ok"])
        self.p_kline.assert_called_with("IFL9", category="1min", count=240)

    def test_kline_route_uses_cache_within_ttl(self):
        self.client.get("/futures/exquote/kline/IFL9?period=1min&count=240&ttl=15")
        self.client.get("/futures/exquote/kline/IFL9?period=1min&count=240&ttl=15")
        # ttl 内第二次请求命中缓存，不回查数据层
        self.assertEqual(self.p_kline.call_count, 1)

    # ---- 不可用兜底 ----
    def test_routes_return_unavailable_when_exhq_down(self):
        fut._EXHQ_OK = False
        for path in (
            "/futures/snapshot",
            "/futures/exquote/minute/IFL9",
            "/futures/exquote/quote/IFL9",
            "/futures/exquote/tick/IFL9",
            "/futures/exquote/kline/IFL9",
        ):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200)
            body = json.loads(resp.get_data(as_text=True))
            self.assertFalse(body.get("ok", True))


if __name__ == "__main__":
    unittest.main()
