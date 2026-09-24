# -*- coding: utf-8 -*-
"""core/futures_service 单元测试。

通过 monkeypatch 把 _get_api 替换为返回内存假对象，避免依赖外部通达信扩展行情
服务器网络；重点验证：
  - 分时 / 盘口 / 逐笔 的字段归一化；
  - 期指逐笔价格 1/1000 还原口径（与参考源 E:\\github\\tdx_flask 的 1/10000 不同）；
  - direction -> buyorsell 映射；
  - 连接/查询异常时的重试与兜底（返回空容器而非抛错）；
  - 数据源判据 ext_available() 只看 _TDX_EXT_SERVERS 池 + TdxExHq_API（与旧 tdx_exhq 包解耦）。
"""
import unittest
from types import SimpleNamespace

import core.futures_service as fut


class FakeExHqApi:
    """模拟 TdxExHq_API：返回构造好的行情记录，可按需模拟异常。"""

    def __init__(self, minute_rows=None, quote=None, ticks=None, bars=None, fail_first_get_minute=False):
        self.minute_rows = minute_rows or []
        self.quote = quote or []
        self.ticks = ticks or []
        self.bars = bars or []
        self.fail_first_get_minute = fail_first_get_minute
        self._minute_calls = 0
        self.last_bars_cat = None
        self.last_bars_market = None
        # _get_api 会调用 api.client.settimeout(...)
        self.client = SimpleNamespace(settimeout=lambda t: None)
        self.disconnected = False

    def get_minute_time_data(self, market, code):
        if self.fail_first_get_minute:
            self._minute_calls += 1
            if self._minute_calls == 1:
                raise RuntimeError("simulated exquote failure")
        return self.minute_rows

    def get_instrument_quote(self, market, code):
        return self.quote

    def get_transaction_data(self, market, code, start=0, count=10):
        return self.ticks

    def get_instrument_bars(self, cat, market, code, start=0, count=700):
        # 记录下发的 category / market，用于验证周期映射
        self.last_bars_cat = cat
        self.last_bars_market = market
        return self.bars

    def disconnect(self):
        self.disconnected = True


def _make_minute_rows():
    return [
        {"hour": 9, "minute": 31, "price": 4556.0, "avg_price": 4555.0, "volume": 2439},
        {"hour": 9, "minute": 32, "price": 4557.2, "avg_price": 4555.6, "volume": 1200},
    ]


def _make_quote():
    q = {
        "price": 4555.4,
        "pre_close": 4556.4,
        "open": 4556.0,
        "high": 4574.4,
        "low": 4549.2,
        "chicang": 253128.0,
        "zongliang": 47847.0,
    }
    for i in range(1, 6):
        q["bid%d" % i] = 4555.0 - i
        q["bid_vol%d" % i] = float(i * 2)
        q["ask%d" % i] = 4556.0 + i
        q["ask_vol%d" % i] = float(i * 3)
    return [q]


def _make_ticks():
    # 价格字段为原始整数（1/1000 元）：4555600 -> 4555.6
    return [
        {"hour": 11, "minute": 30, "second": 1, "price": 4555600, "volume": 2, "direction": 1},
        {"hour": 11, "minute": 30, "second": 2, "price": 4555400, "volume": 5, "direction": -1},
        {"hour": 11, "minute": 30, "second": 3, "price": 4555500, "volume": 1, "direction": 0},
    ]


def _make_bars():
    # get_instrument_bars 原始记录：datetime 为 'YYYY-MM-DD HH:MM'，
    # trade=成交量(手)，position=持仓(手)。
    return [
        {"datetime": "2026-08-28 09:31", "open": 4555.0, "high": 4556.0, "low": 4554.0,
         "close": 4555.5, "trade": 120, "position": 253000},
        {"datetime": "2026-08-28 09:32", "open": 4555.5, "high": 4557.0, "low": 4555.0,
         "close": 4556.2, "trade": 90, "position": 253100},
    ]


class FuturesServiceTest(unittest.TestCase):

    def setUp(self):
        # 服务器源已切换为 _TDX_EXT_SERVERS 池：直接替换 _get_api 返回内存假对象，
        # 不再走 connect_exhq（旧 tdx_exhq 包已不参与取数）。
        self._orig_get_api = fut._get_api
        self._orig_exhq_ok = fut._EXHQ_OK
        self._orig_src_ok = fut._EXT_SRC_OK
        self._orig_api_ok = fut._EXT_API_OK
        fut._EXHQ_OK = True
        fut._EXT_SRC_OK = True
        fut._EXT_API_OK = True
        self.api = None
        fut._get_api = lambda: self.api

    def tearDown(self):
        fut._get_api = self._orig_get_api
        fut._EXHQ_OK = self._orig_exhq_ok
        fut._EXT_SRC_OK = self._orig_src_ok
        fut._EXT_API_OK = self._orig_api_ok

    def _install(self, **kwargs):
        self.api = FakeExHqApi(**kwargs)
        return self.api

    # ---- 分时 ----
    def test_fut_minute_normalizes(self):
        self._install(minute_rows=_make_minute_rows())
        rows = fut.fut_minute("IFL9")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["time"], "09:31")
        self.assertEqual(rows[0]["price"], 4556.0)
        self.assertEqual(rows[0]["avg_price"], 4555.0)
        self.assertEqual(rows[0]["vol"], 2439)
        self.assertEqual(rows[1]["time"], "09:32")

    def test_fut_minute_empty(self):
        self._install(minute_rows=[])
        self.assertEqual(fut.fut_minute("IFL9"), [])

    # ---- 盘口 ----
    def test_fut_quote_normalizes(self):
        self._install(quote=_make_quote())
        q = fut.fut_quote("IFL9")
        self.assertEqual(q["price"], 4555.4)
        self.assertEqual(q["last_close"], 4556.4)
        self.assertEqual(q["open"], 4556.0)
        self.assertEqual(q["high"], 4574.4)
        self.assertEqual(q["low"], 4549.2)
        self.assertEqual(q["chicang"], 253128.0)
        self.assertEqual(q["zongliang"], 47847.0)
        self.assertEqual(len(q["bids"]), 5)
        self.assertEqual(len(q["asks"]), 5)
        self.assertEqual(q["bids"][0], [4554.0, 2.0])
        self.assertEqual(q["asks"][0], [4557.0, 3.0])
        self.assertTrue(q["servertime"])  # 形如 HH:MM:SS

    def test_fut_quote_handles_empty(self):
        self._install(quote=[])
        self.assertEqual(fut.fut_quote("IFL9"), {})

    # ---- 逐笔：关键口径 ----
    def test_fut_tick_divides_price_by_1000(self):
        self._install(ticks=_make_ticks())
        ticks = fut.fut_tick("IFL9", count=40)
        self.assertEqual(len(ticks), 3)
        # 原始 4555600 -> 4555.6
        self.assertEqual(ticks[0]["price"], 4555.6)
        self.assertEqual(ticks[0]["time"], "11:30:01")
        self.assertEqual(ticks[0]["vol"], 2)
        self.assertEqual(ticks[0]["num"], 1)

    def test_fut_tick_direction_mapping(self):
        self._install(ticks=_make_ticks())
        ticks = fut.fut_tick("IFL9", count=40)
        # direction 1 => 外盘/买 => 0
        self.assertEqual(ticks[0]["buyorsell"], 0)
        # direction -1 => 内盘/卖 => 1
        self.assertEqual(ticks[1]["buyorsell"], 1)
        # direction 0 => 中性 => 2
        self.assertEqual(ticks[2]["buyorsell"], 2)

    def test_fut_tick_returns_tail(self):
        self._install(ticks=_make_ticks())
        # count 小于总量时只取末尾
        ticks = fut.fut_tick("IFL9", count=1)
        self.assertEqual(len(ticks), 1)
        self.assertEqual(ticks[0]["price"], 4555.5)

    def test_fut_tick_empty(self):
        self._install(ticks=[])
        self.assertEqual(fut.fut_tick("IFL9", count=40), [])

    # ---- 异常与重试 ----
    def test_query_exception_returns_empty(self):
        self._install(minute_rows=_make_minute_rows())

        def _boom(market, code):
            raise RuntimeError("simulated exquote failure")

        self.api.get_minute_time_data = _boom
        self.assertEqual(fut.fut_minute("IFL9"), [])

    def test_safe_query_retries_once_then_succeeds(self):
        # 第一次 get_minute_time_data 抛错，第二次（重试用的新连接）成功
        self._install(minute_rows=_make_minute_rows(), fail_first_get_minute=True)
        rows = fut.fut_minute("IFL9")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["time"], "09:31")

    def test_connection_failure_returns_empty(self):
        # 池内所有节点都连不上 -> _get_api 返回 None -> 三个查询都返回空容器
        self._install(minute_rows=_make_minute_rows())
        fut._get_api = lambda: None
        self.assertEqual(fut.fut_minute("IFL9"), [])
        self.assertEqual(fut.fut_quote("IFL9"), {})
        self.assertEqual(fut.fut_tick("IFL9"), [])

    # ---- 数据源判据 ext_available()（隐患回归）----
    def test_ext_available_ignores_legacy_tdx_exhq(self):
        """旧 tdx_exhq 包不可用（_EXHQ_OK=False）不应影响新数据源判据。"""
        fut._EXHQ_OK = False
        fut._EXT_SRC_OK = True
        fut._EXT_API_OK = True
        self.assertTrue(fut.ext_available())

    def test_ext_available_requires_pool_and_api(self):
        fut._EXHQ_OK = True
        fut._EXT_SRC_OK = False
        fut._EXT_API_OK = True
        self.assertFalse(fut.ext_available())
        fut._EXT_SRC_OK = True
        fut._EXT_API_OK = False
        self.assertFalse(fut.ext_available())

    def test_get_api_returns_none_when_pool_unavailable(self):
        """数据源不可用时 _get_api 不建连直接返回 None（不抛错）。"""
        fut._EXT_SRC_OK = False
        self.assertIsNone(self._orig_get_api())
        self.assertEqual(fut.fut_quote("IFL9"), {})

    # ---- K线（历史 get_instrument_bars）----
    def test_fut_bars_normalizes(self):
        api = self._install(bars=_make_bars())
        bars = fut.fut_bars("IFL9", category="1min", count=240)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["date"], "2026-08-28 09:31")
        self.assertEqual(bars[0]["open"], 4555.0)
        self.assertEqual(bars[0]["high"], 4556.0)
        self.assertEqual(bars[0]["low"], 4554.0)
        self.assertEqual(bars[0]["close"], 4555.5)
        self.assertEqual(bars[0]["vol"], 120)
        self.assertEqual(bars[0]["position"], 253000)
        # 1min -> category 8，market 为 CFFEX(47)
        self.assertEqual(api.last_bars_cat, 8)
        self.assertEqual(api.last_bars_market, 47)

    def test_fut_bars_category_mapping(self):
        api = self._install(bars=_make_bars())
        fut.fut_bars("IFL9", category="1min")
        self.assertEqual(api.last_bars_cat, 8)
        fut.fut_bars("IFL9", category="5min")
        self.assertEqual(api.last_bars_cat, 0)
        fut.fut_bars("IFL9", category="30min")
        self.assertEqual(api.last_bars_cat, 2)
        # 默认 1min
        fut.fut_bars("IFL9")
        self.assertEqual(api.last_bars_cat, 8)
        # 非法周期回退到 1min
        fut.fut_bars("IFL9", category="99min")
        self.assertEqual(api.last_bars_cat, 8)

    def test_fut_bars_skips_malformed(self):
        bad = [
            {"datetime": "2026-08-28 09:31", "open": 4555.0, "high": 4556.0,
             "low": 4554.0, "close": 4555.5, "trade": 120, "position": 253000},
            # 非法 OHLC 字段，单条应被跳过而非整体失败
            {"datetime": "bad", "open": "x", "high": 1, "low": 0, "close": 1},
        ]
        self._install(bars=bad)
        bars = fut.fut_bars("IFL9")
        self.assertEqual(len(bars), 1)

    def test_fut_bars_empty(self):
        self._install(bars=[])
        self.assertEqual(fut.fut_bars("IFL9", category="5min"), [])

    def test_fut_bars_exception_returns_empty(self):
        self._install(bars=_make_bars())

        def _boom(cat, market, code, start=0, count=700):
            raise RuntimeError("simulated exquote failure")

        self.api.get_instrument_bars = _boom
        self.assertEqual(fut.fut_bars("IFL9", category="30min"), [])


if __name__ == "__main__":
    unittest.main()
