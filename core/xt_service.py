# -*- coding: utf-8 -*-
"""xtquant 行情/交易服务封装。
   - 行情：直接用 xtdata（无需 miniQMT 交易连接）
   - 交易：根据 Config.TRADE_BACKEND 走 xttrader / HTTP 桥 / dry
"""
import logging
from typing import Any, Dict, List

from config import Config

log = logging.getLogger(__name__)

# ===================== 行情 =====================
def get_quote(code: str) -> Dict[str, Any]:
    """取最新 tick 快照。code: '600000.SH'"""
    from xtquant import xtdata
    xtdata.subscribe_quote(code, period="tick")
    data = xtdata.get_market_data_ex([], [code], period="tick", count=1)
    if code not in data:
        return {}
    df = data[code]
    if df is None or len(df) == 0:
        return {}
    r = df.iloc[-1].to_dict()
    r["code"] = code
    return r


def get_kline(code: str, period: str = "1d", count: int = 120) -> List[Dict[str, Any]]:
    """取K线。period: '1d'/'1m'/'5m'/'15m'/'60m'"""
    from xtquant import xtdata
    data = xtdata.get_market_data_ex([], [code], period=period, count=count)
    if code not in data:
        return []
    df = data[code]
    if df is None or len(df) == 0:
        return []
    df = df.reset_index().rename(columns={"index": "time", "time": "time"})
    # 时间转字符串
    if "time" in df.columns:
        df["time"] = df["time"].astype(str)
    return df.to_dict(orient="records")


# ===================== 交易（后端可切换） =====================
class _DryTrader:
    def buy(self, code, vol, price, **kw):
        log.info("[DRY] buy %s x%d @%s", code, vol, price)
        return {"ok": True, "dry": True, "orderid": "DRY-" + code}
    def sell(self, code, vol, price, **kw):
        log.info("[DRY] sell %s x%d @%s", code, vol, price)
        return {"ok": True, "dry": True, "orderid": "DRY-" + code}
    def cancel(self, orderid):
        log.info("[DRY] cancel %s", orderid)
        return {"ok": True, "dry": True}
    def asset(self):      return [{"total": 0.0, "cash": 0.0, "market_value": 0.0}]
    def positions(self):  return []
    def orders(self):     return []
    def trades(self):     return []


class _XtTraderTrader:
    """直连 miniQMT 的 XtQuantTrader。"""
    def __init__(self):
        from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
        from xtquant.xttype import StockAccount
        self._acc = StockAccount(Config.XT_ACCOUNT_ID, Config.XT_ACCOUNT_TYPE)
        self._tr = XtQuantTrader(Config.XT_PATH, Config.XT_SESSION_ID)
        self._tr.register_callback(XtQuantTraderCallback())
        ret = self._tr.start()
        # 注意：connect/start 返回 0 = 成功（C 风格错误码）
        if isinstance(ret, int) and ret != 0:
            raise RuntimeError(f"XtQuantTrader.start() 失败 code={ret}")
        self._tr.subscribe(self._acc)
    def buy(self, code, vol, price, **kw):
        oid = self._tr.order_stock_async(self._acc, code, 23, int(vol), 1, float(price),
                                         strategy_name=kw.get("name", "flask"), order_remark="buy")
        return {"ok": True, "orderid": oid}
    def sell(self, code, vol, price, **kw):
        oid = self._tr.order_stock_async(self._acc, code, 24, int(vol), 1, float(price),
                                         strategy_name=kw.get("name", "flask"), order_remark="sell")
        return {"ok": True, "orderid": oid}
    def cancel(self, orderid):
        self._tr.cancel_order_stock_async(self._acc, orderid)
        return {"ok": True}
    def asset(self):
        a = self._tr.query_stock_asset(self._acc)
        return [] if not a else [{"total": a.total_asset, "cash": a.cash, "market_value": a.market_value}]
    def positions(self):
        ps = self._tr.query_stock_positions(self._acc) or []
        return [{"code": p.stock_code, "vol": p.volume, "can_use": p.can_use_volume,
                 "cost": p.open_price, "mv": p.market_value} for p in ps]
    def orders(self):
        os_ = self._tr.query_stock_orders(self._acc) or []
        return [{"orderid": o.order_id, "code": o.stock_code, "status": o.order_status,
                 "vol": o.order_volume, "price": o.order_price, "type": o.order_type} for o in os_]
    def trades(self):
        ts = self._tr.query_stock_trades(self._acc) or []
        return [{"tradeid": t.traded_id, "code": t.stock_code, "vol": t.traded_volume,
                 "price": t.traded_price} for t in ts]


class _BridgeTrader:
    """走之前写的 HTTP 桥（路径 C）。"""
    def __init__(self):
        import requests
        self._url = Config.BRIDGE_URL
        self._h = {"X-API-Token": Config.BRIDGE_TOKEN, "Content-Type": "application/json"}
        self._s = requests.Session()
    def _get(self, p):
        r = self._s.get(self._url + p, headers=self._h, timeout=10); return r.json()
    def _post(self, p, b):
        r = self._s.post(self._url + p, json=b, headers=self._h, timeout=10); return r.json()
    def buy(self, code, vol, price, **kw):
        return self._post("/order", {"order_type": "buy", "stock_code": code,
                                      "volume": int(vol), "price_type": "fix", "price": float(price)})
    def sell(self, code, vol, price, **kw):
        return self._post("/order", {"order_type": "sell", "stock_code": code,
                                      "volume": int(vol), "price_type": "fix", "price": float(price)})
    def cancel(self, orderid):
        return self._post("/cancel", {"orderid": orderid})
    def asset(self):     return self._get("/asset").get("data", [])
    def positions(self): return self._get("/positions").get("data", [])
    def orders(self):    return self._get("/orders").get("data", [])
    def trades(self):    return self._get("/trades").get("data", [])


_trader = None
def get_trader():
    global _trader
    if _trader is None:
        bk = Config.TRADE_BACKEND
        if bk == "xtquant":
            _trader = _XtTraderTrader()
        elif bk == "bridge":
            _trader = _BridgeTrader()
        else:
            _trader = _DryTrader()
        log.info("trader backend = %s", bk)
    return _trader
