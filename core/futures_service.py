# -*- coding: utf-8 -*-
"""股指期货(中金所 CFFEX)扩展行情(ExHq) 盘口 / 分时 / 逐笔服务。

数据源（与期权扩展行情同源，pip 包，已安装，无需拷贝源码）：
  - pytdx_patches ：对 pytdx 的分时 / 历史K线 / 心跳等接口打补丁（国信、国元专有扩展行情）。
  - tdx_exhq      ：封装 TdxExHq_API 连接（EXHQ_SERVERS 含国信7721 / 国元7721 / 通用7727）。

期指合约（当月连续）固定在 CFFEX，扩展行情市场代码 = 47；合约代码 IFL9/IHL9/ICL9/IML9
即为 TDX 查询 code（无需像期权那样做 8 位 → 数字ID 映射）。

对外归一化为与「可转债看盘板 / 期权监控」board-rt 完全一致的数据契约：
  - 分时  fut_minute(code)  -> [{"time":"HH:MM","price":元,"avg_price":元,"vol":手(当分钟)}, ...]
  - 盘口  fut_quote(code)   -> {"price","last_close","open","high","low","chicang","zongliang","servertime","bids":[[价,量]×5],"asks":[[价,量]×5]}
  - 逐笔  fut_tick(code)    -> [{"time":"HH:MM:SS","price":元,"vol":手,"num":1,"buyorsell":0/1/2}, ...]

关键口径（已对线上行情实测确认）：
  - 期指 get_transaction_data 价格单位为 1/1000 元（期权为 1/10000），需 ÷1000 还原为元。
  - direction: 1=外盘/买, -1=内盘/卖, 0=中性。
  - 盘口 / 分时价格直接为元；chicang=持仓(手)，zongliang=当日总成交量(手)。
"""
import time

# 必须先 import pytdx_patches 以应用补丁，再 import tdx_exhq（其 TdxExHq_API 复用被补丁过的解析器）。
try:
    import pytdx_patches  # noqa: F401
except Exception:
    pass

try:
    from tdx_exhq import connect_exhq
    _EXHQ_OK = True
except Exception:
    connect_exhq = None
    _EXHQ_OK = False

# 中金所 CFFEX 扩展行情市场代码
FUTURES_MARKET = 47

# 股指期货主力当月连续合约（用户指定：IFL9 / IHL9 / ICL9 / IML9）
FUTURES = [
    ("IFL9", "沪深300当月连续"),
    ("IHL9", "上证50当月连续"),
    ("ICL9", "中证500当月连续"),
    ("IML9", "中证1000当月连续"),
]


# =========================================================
# 每次查询新建连接（避免长连接被服务端丢弃后 recv 挂死）
# =========================================================
_EXHQ_TIMEOUT = 6.0


def _f(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def _get_api():
    """新建一个扩展行情连接；失败返回 None。每次查询都新建，避免长连接被服务端丢弃后 recv 挂死。"""
    if not _EXHQ_OK or connect_exhq is None:
        return None
    try:
        api = connect_exhq(time_out=_EXHQ_TIMEOUT)
        if api is not None:
            # 收紧 socket 超时，避免服务端丢弃空闲连接后 recv 长时间阻塞。
            try:
                api.client.settimeout(_EXHQ_TIMEOUT)
            except Exception:
                pass
        return api
    except Exception:
        return None


def _safe_query(fn):
    """执行一次 ExHq 查询；连接异常时直接换新连接重试一次，结束后释放 socket。"""
    api = _get_api()
    if api is None:
        return None
    result = None
    try:
        result = fn(api)
    except Exception:
        try:
            api.disconnect()
        except Exception:
            pass
        api2 = _get_api()
        if api2 is not None:
            try:
                result = fn(api2)
            except Exception:
                result = None
            finally:
                try:
                    api2.disconnect()
                except Exception:
                    pass
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    return result


# ExHq 历史K线周期 -> get_instrument_bars category 映射（pytdx.params.TDXParams.KLINE_TYPE_*）
#   1min = KLINE_TYPE_1MIN(8), 5min = KLINE_TYPE_5MIN(0), 30min = KLINE_TYPE_30MIN(2)
_FUT_BARS_CATEGORY = {
    "1min": 8,
    "5min": 0,
    "30min": 2,
}


def fut_bars(code, category="1min", count=240):
    """期指历史K线：ExHq get_instrument_bars（category 在前，market/code 在后）。

    category 支持 '1min' / '5min' / '30min'（默认 1min）。
    返回 [{'date','open','high','low','close','vol','position'}, ...}：
      - OHLC 单位元；vol=成交量(手)；position=持仓(手)。
      - 原始字段 price(恒为0) / amount(异常值) 为冗余/错误字段，已舍弃。
    """
    cat = _FUT_BARS_CATEGORY.get(category, _FUT_BARS_CATEGORY["1min"])

    def fn(api):
        bars = api.get_instrument_bars(cat, FUTURES_MARKET, code, 0, count)
        out = []
        for d in (bars or []):
            try:
                out.append({
                    "date": str(d.get("datetime") or ""),
                    "open": float(d["open"]),
                    "high": float(d["high"]),
                    "low": float(d["low"]),
                    "close": float(d["close"]),
                    "vol": int(d.get("trade") or 0),
                    "position": int(d.get("position") or 0),
                })
            except Exception:
                continue
        return out

    return _safe_query(fn) or []


# =========================================================
# 归一化接口（board-rt 数据契约）
# =========================================================
def fut_minute(code):
    """当日分时（1 分钟）：ExHq get_minute_time_data。vol 为当分钟成交量(手)。"""
    def fn(api):
        rows = api.get_minute_time_data(FUTURES_MARKET, code)
        out = []
        for d in (rows or []):
            try:
                out.append({
                    "time": "%02d:%02d" % (int(d["hour"]), int(d["minute"])),
                    "price": float(d["price"]),
                    "avg_price": float(d.get("avg_price") or d.get("price") or 0),
                    "vol": int(d.get("volume") or 0),
                })
            except Exception:
                continue
        return out

    return _safe_query(fn) or []


def fut_quote(code):
    """五档买卖盘口 + 持仓/成交量：ExHq get_instrument_quote。"""
    def fn(api):
        q = api.get_instrument_quote(FUTURES_MARKET, code)
        # 接口返回 [OrderedDict(...)]（单元素列表），需解包；也兼容直接返回 dict。
        if isinstance(q, (list, tuple)):
            q = q[0] if q else None
        if not q:
            return {}
        bids, asks = [], []
        for i in range(1, 6):
            bids.append([_f(q.get("bid%d" % i)), _f(q.get("bid_vol%d" % i))])
            asks.append([_f(q.get("ask%d" % i)), _f(q.get("ask_vol%d" % i))])
        return {
            "price": _f(q.get("price")),
            "last_close": _f(q.get("pre_close")),
            "open": _f(q.get("open")),
            "high": _f(q.get("high")),
            "low": _f(q.get("low")),
            "chicang": _f(q.get("chicang")),
            "zongliang": _f(q.get("zongliang")),
            "servertime": time.strftime("%H:%M:%S"),
            "bids": bids,
            "asks": asks,
        }

    return _safe_query(fn) or {}


def fut_tick(code, count=40):
    """逐笔成交：ExHq get_transaction_data（取最近 count 笔）。
    期指 get_transaction_data 价格单位为 1/1000 元（期权为 1/10000），需 ÷1000 还原。
    direction: 1=外盘/买, -1=内盘/卖, 0=中性。
    """
    def fn(api):
        rows = api.get_transaction_data(FUTURES_MARKET, code, start=0, count=1800)
        if not rows:
            return []
        rows = rows[-count:]
        out = []
        for d in rows:
            try:
                h = int(d["hour"]); m = int(d["minute"]); s = int(d.get("second") or 0)
                direction = int(d.get("direction") or 0)
                bs = 0 if direction > 0 else (1 if direction < 0 else 2)
                out.append({
                    "time": "%02d:%02d:%02d" % (h, m, s),
                    # 期指 get_transaction_data 价格单位为 1/1000 元（分时/盘口为 元），需还原为元
                    "price": float(d["price"]) / 1000.0,
                    "vol": int(d.get("volume") or 0),
                    "num": 1,
                    "buyorsell": bs,
                })
            except Exception:
                continue
        return out

    return _safe_query(fn) or []
