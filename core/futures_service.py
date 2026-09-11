# -*- coding: utf-8 -*-
"""股指期货(中金所 CFFEX)扩展行情(ExHq) 盘口 / 分时 / 逐笔服务。

数据源：
  - core/tdx_ext_servers._TDX_EXT_SERVERS：扩展行情主站池（端口 7721，当前 4 台长城证券），
    由 _get_api() 随机优选试连、失败自动换下一台。取数用 pytdx.exhq.TdxExHq_API。
  - 旧 pip 包 tdx_exhq 已不再参与取数（仅为向后兼容保留导入与 _EXHQ_OK 标记，
    可用性判据请用 ext_available()）。

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
import threading

# 应用 pytdx_patches 补丁（get_security_bars P4 / 心跳自愈），须在 TdxExHq_API 使用前导入。
try:
    import pytdx_patches  # noqa: F401
except Exception:
    pass

# 旧 pip 包 tdx_exhq：已不参与取数（服务器源改为 _TDX_EXT_SERVERS 池）。
# 仅保留导入与 _EXHQ_OK 标记作向后兼容；可用性判据请一律用 ext_available()。
try:
    from tdx_exhq import connect_exhq
    _EXHQ_OK = True
except Exception:
    connect_exhq = None
    _EXHQ_OK = False

# 扩展行情服务器源：与期权共用 core/tdx_ext_servers._TDX_EXT_SERVERS
# （长城4+国元5+国信3+川财2 的 7721 共 14 台，通达信官方 7727 一台，
#   另有 GitHub 来源 中信4+华泰8+国君4 的 7721 共 16 台、银河 7730 一台，合计 32 台）。
# 取代 tdx_exhq 内置的 EXHQ_SERVERS（国信/国元7721 + 通用7727），由本池单一管控，期权与之同源。
try:
    from core.tdx_ext_servers import _TDX_EXT_SERVERS, _PYTDX_EXT_SERVERS
    _EXT_SRC_OK = True
except Exception:
    try:
        from tdx_ext_servers import _TDX_EXT_SERVERS, _PYTDX_EXT_SERVERS
        _EXT_SRC_OK = True
    except Exception:
        _TDX_EXT_SERVERS = []
        _PYTDX_EXT_SERVERS = []
        _EXT_SRC_OK = False

try:
    from pytdx.exhq import TdxExHq_API
    _EXT_API_OK = True
except Exception:
    TdxExHq_API = None
    _EXT_API_OK = False

# 池选路器：延迟排序优先 + 故障节点临时降权（与期权共用同一份统计，见 core/tdx_pool_router）
try:
    from core import tdx_pool_router as _router
except Exception:
    try:
        import tdx_pool_router as _router
    except Exception:
        _router = None


def ext_available():
    """扩展行情数据源是否可用（HTTP 层判据统一以此为准）。

    判据 = 「新数据源 _TDX_EXT_SERVERS 池就绪（_EXT_SRC_OK）」且「TdxExHq_API 可用（_EXT_API_OK）」。

    历史坑：此前 api/futures_bp._exq_unavailable() 用的是旧 pip 包 tdx_exhq 的导入标志
    _EXHQ_OK。但服务器源早已切换为 core/tdx_ext_servers 池，旧包不再是取数依赖——
    一旦 tdx_exhq 被卸载，全部期指 HTTP 接口会被误判为不可用（尽管 7721 池完全可用）。
    故对外暴露本函数作为唯一判据；_EXHQ_OK 仅保留作向后兼容标记，不参与门禁。
    """
    return bool(_EXT_SRC_OK and _EXT_API_OK and TdxExHq_API is not None)


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

# =========================================================
# 并发控制：singleflight 合并 + 全局连接信号量限流
# =========================================================
# 对外并发取数连接数上限：防止突发把 32 台券商扩展行情主站单台连接打爆（雪崩）。
# 每次查询仍「新建连接」以规避长连接被服务端丢弃后 recv 挂死，仅在此封顶总数。
_EXHQ_MAX_CONCURRENT = 20
_EXHQ_SEM = threading.Semaphore(_EXHQ_MAX_CONCURRENT)

# singleflight：同 key 并发只放行一个真正取数，其余等待复用其结果；
# 取数完成后 _SF_COALESCE 秒内再来同一 key 直接复用，避免微错峰的突发重复打网络。
_SF = {}
_SF_LOCK = threading.Lock()
_SF_COALESCE = 0.25


def _fetch_coalesced(key, query_fn):
    """同 key 并发取数合并（singleflight）。

    - key 相同且正在取数：等待复用，不重复建连。
    - key 已完成且在 _SF_COALESCE 合并窗口内：直接复用，不重复取数。
    - 否则成为本轮唯一取数者，执行 query_fn（内部已受 _EXHQ_SEM 限流）。
    """
    now = time.time()
    with _SF_LOCK:
        slot = _SF.get(key)
        if slot is None or (slot["event"].is_set() and now - slot["ts"] > _SF_COALESCE):
            ev = threading.Event()
            _SF[key] = {"event": ev, "result": None, "ts": 0.0}
            producer = True
            wait_slot = None
        elif not slot["event"].is_set():
            producer = False
            ev = slot["event"]
            wait_slot = slot
        else:
            return slot["result"]  # 合并窗口内直接复用
    if not producer:
        ev.wait()
        return wait_slot.get("result")
    try:
        result = query_fn()
    finally:
        with _SF_LOCK:
            s = _SF.get(key)
            if s is not None:
                s["result"] = result
                s["ts"] = time.time()
                s["event"].set()
                _SF.pop(key, None)
    return result


def _f(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def _probe_latency(host, port, timeout=3.0):
    """预热探测：连一次即断开，返回耗时毫秒；失败返回 None。
    只负责测量，成败由 tdx_pool_router 统一上报（避免双重计数）。"""
    try:
        api = TdxExHq_API()
    except Exception:
        return None
    t0 = time.time()
    try:
        if not api.connect(host, port, time_out=timeout):
            return None
        return (time.time() - t0) * 1000.0
    except Exception:
        return None
    finally:
        try:
            api.disconnect()
        except Exception:
            pass


def _get_api():
    """新建一个扩展行情连接（池内按「延迟排序优先 + 故障降权」选路，失败换下一台）；失败返回 None。
    每次查询都新建，避免长连接被服务端丢弃后 recv 挂死。

    选路由 core/tdx_pool_router 统一负责：优先挑 EMA 延迟低的节点，连续失败的节点会被
    临时冷却（指数退避），冷却到期自动回到候选池。若 router 不可用则退化为随机洗牌。"""
    if not ext_available():
        return None
    import random
    pairs = list(_PYTDX_EXT_SERVERS)
    if not pairs:
        return None
    if _router is not None:
        # 冷启动/间隔到期时后台异步测一遍延迟（非阻塞）
        try:
            _router.maybe_warmup(pairs, _probe_latency)
        except Exception:
            pass
        order = _router.select(pairs)
    else:
        order = list(pairs)
        random.shuffle(order)
    for host, port in order:
        api = None
        t0 = time.time()
        try:
            api = TdxExHq_API()
            if not api.connect(host, port, time_out=_EXHQ_TIMEOUT):
                try:
                    api.disconnect()
                except Exception:
                    pass
                if _router:
                    _router.report_failure(host, port)
                continue
            # 收紧 socket 超时，避免服务端丢弃空闲连接后 recv 长时间阻塞。
            try:
                api.client.settimeout(_EXHQ_TIMEOUT)
            except Exception:
                pass
            if _router:
                _router.report_success(host, port, (time.time() - t0) * 1000.0)
            # 记住实际连上的节点，供取数失败时精确定位降权
            try:
                api._pool_addr = (host, port)
            except Exception:
                pass
            return api
        except Exception:
            try:
                if api is not None:
                    api.disconnect()
            except Exception:
                pass
            if _router:
                _router.report_failure(host, port)
            continue
    return None


def _report_api_failure(api):
    """取数中途失败时，对实际连上的那台节点上报故障（供选路器降权）。"""
    if _router is None or api is None:
        return
    addr = getattr(api, "_pool_addr", None)
    if not addr:
        return
    try:
        _router.report_failure(addr[0], addr[1])
    except Exception:
        pass


def _safe_query(fn):
    """执行一次 ExHq 查询（受全局连接信号量 _EXHQ_SEM 限流，防止并发打爆券商扩展行情主站）；
    连接异常时换新连接重试一次，结束后释放 socket。失败节点会被选路器降权。"""
    if not ext_available():
        return None

    def _work():
        api = _get_api()
        if api is None:
            return None
        try:
            return fn(api)
        except Exception:
            _report_api_failure(api)
            try:
                api.disconnect()
            except Exception:
                pass
            api2 = _get_api()
            if api2 is not None:
                try:
                    return fn(api2)
                except Exception:
                    _report_api_failure(api2)
                    return None
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
        return None

    with _EXHQ_SEM:
        return _work()


# ExHq 历史K线周期 -> get_instrument_bars category 映射（pytdx.params.TDXParams.KLINE_TYPE_*）
#   1min = KLINE_TYPE_1MIN(8), 5min = KLINE_TYPE_5MIN(0), 30min = KLINE_TYPE_30MIN(2)
#   60min = KLINE_TYPE_1HOUR(3), day = KLINE_TYPE_DAILY(4), week = KLINE_TYPE_WEEKLY(5), month = KLINE_TYPE_MONTHLY(6)
_FUT_BARS_CATEGORY = {
    "1min": 8,
    "5min": 0,
    "30min": 2,
    "60min": 3,
    "day": 4,
    "week": 5,
    "month": 6,
}


def fut_bars(code, category="1min", count=240):
    """期指历史K线：ExHq get_instrument_bars（category 在前，market/code 在后）。

    category 支持 '1min' / '5min' / '30min' / '60min' / 'day' / 'week' / 'month'（默认 1min）。
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

    return _fetch_coalesced("fut_bars:%s:%s:%d" % (code, category, count), lambda: _safe_query(fn)) or []


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

    return _fetch_coalesced("fut_minute:%s" % code, lambda: _safe_query(fn)) or []


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

    return _fetch_coalesced("fut_quote:%s" % code, lambda: _safe_query(fn)) or {}


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

    return _fetch_coalesced("fut_tick:%s" % code, lambda: _safe_query(fn)) or []
