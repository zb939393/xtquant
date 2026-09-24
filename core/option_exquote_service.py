# -*- coding: utf-8 -*-
"""期权扩展行情(ExHq)盘口 / 分时 / 逐笔服务。

数据源（统一扩展行情池，端口 7721）：
  - core/tdx_ext_servers._TDX_EXT_SERVERS：与 futures_service 共用的同一份服务器池（32 台）。
    建连自管（TdxExHq_API(heartbeat=True, auto_retry=True)，与 tdx_exhq.connect_exhq 同参数），
    选路由 core/tdx_pool_router 统一负责：延迟排序优先 + 故障节点临时降权，与期指共用统计。
  - tdx_exhq：仅用于扩展行情连接封装与期权合约枚举（get_option_codes），
    其内置 EXHQ_SERVERS 默认服务器列表**已被本池取代**。

对外归一化为与「可转债看盘板」board-rt 完全一致的数据契约：
  - 分时  exquote_minute(code)  -> [{"time":"HH:MM","price":元,"avg_price":元,"vol":张}, ...]
  - 盘口  exquote_quote(code)   -> {"price","last_close","servertime","bids":[[价,量]×5],"asks":[[价,量]×5]}
  - 逐笔  exquote_tick(code)    -> [{"time":"HH:MM:SS","price":元,"vol":张,"num":1,"buyorsell":0/1/2}, ...]

代码桥接：watchlist 存储的 8 位合约代码即 TDX 扩展行情数字合约ID：
  SSE 期权(100xxxxx) -> 市场 8 ； SZSE 期权(900xxxxx) -> 市场 9 。
枚举映射(get_option_codes)可达时优先用于精确校正，不可达时按前缀兜底。
"""
import time
import threading

# 应用 pytdx_patches 补丁（get_security_bars P4 / 心跳自愈），须在 TdxExHq_API 使用前导入。
try:
    import pytdx_patches  # noqa: F401
except Exception:
    pass

try:
    from tdx_exhq import connect_exhq, get_option_codes
    _EXHQ_OK = True
except Exception:
    connect_exhq = None
    get_option_codes = None
    _EXHQ_OK = False

# 直连用裸 API：tdx_exhq.connect_exhq 本质只是「TdxExHq_API(heartbeat, auto_retry) +
# 遍历 servers 连第一台成功的」，且会把 socket 超时硬设为 15s。改为自管连接后，
# 既能沿用 6s 超时，又能把每台节点的连成败精确上报给选路器（延迟排序 + 故障降权）。
try:
    from pytdx.exhq import TdxExHq_API
    _EXT_API_OK = True
except Exception:
    TdxExHq_API = None
    _EXT_API_OK = False

# 统一扩展行情服务器池（与 futures_service 同源）：长城 + 国元 + 国信，端口 7721。
# 取代 tdx_exhq 内置的 EXHQ_SERVERS 默认列表，使期权扩展行情也走这台已验证的池。
try:
    from core.tdx_ext_servers import (
        _TDX_EXT_SERVERS,
        _TDX_EXT_SERVERS_FOR_TDX_EXHQ,
    )
    _EXT_SRC_OK = True
except Exception:
    try:
        from tdx_ext_servers import (
            _TDX_EXT_SERVERS,
            _TDX_EXT_SERVERS_FOR_TDX_EXHQ,
        )
        _EXT_SRC_OK = True
    except Exception:
        _TDX_EXT_SERVERS = []
        _TDX_EXT_SERVERS_FOR_TDX_EXHQ = []
        _EXT_SRC_OK = False

# 池选路器：延迟排序优先 + 故障节点临时降权（与期指共用同一份统计，见 core/tdx_pool_router）
try:
    from core import tdx_pool_router as _router
except Exception:
    try:
        import tdx_pool_router as _router
    except Exception:
        _router = None


def ext_available():
    """期权扩展行情数据源是否可用（HTTP 层判据统一以此为准）。

    判据 = tdx_exhq 可导入（合约枚举 get_option_codes）+ 统一扩展行情池非空 + pytdx 可用。
    与 futures_service.ext_available() 语义一致：都不依赖「某台特定服务器」，只看池与 API。
    """
    return bool(_EXHQ_OK and _EXT_SRC_OK and _EXT_API_OK
                and connect_exhq is not None
                and len(_TDX_EXT_SERVERS_FOR_TDX_EXHQ) > 0)


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


def _pairs():
    """统一池的 (ip, port) 列表（与期指侧同源，故共用同一份选路统计）。"""
    return [(t[0], t[1]) for t in _TDX_EXT_SERVERS_FOR_TDX_EXHQ]


def _ordered_triples(topk=3):
    """按健康度排序的 (ip, port, name) 三元组，供 get_option_codes 等慢调用优先走快节点。"""
    triples = list(_TDX_EXT_SERVERS_FOR_TDX_EXHQ)
    if _router is None or not triples:
        return triples
    try:
        order = _router.select([(t[0], t[1]) for t in triples], topk=topk)
        rank = {}
        for i, k in enumerate(order):
            rank[(str(k[0]), int(k[1]))] = i
        return sorted(triples, key=lambda t: rank.get((str(t[0]), int(t[1])), 10 ** 6))
    except Exception:
        return triples


def _probe_latency(host, port, timeout=3.0):
    """预热探测：连一次即断开，返回耗时毫秒；失败返回 None。
    只负责测量，成败由 tdx_pool_router 统一上报（避免双重计数）。"""
    try:
        api = TdxExHq_API(heartbeat=True, auto_retry=True)
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
    每次查询都新建，避免长连接被服务端丢弃后 recv 挂死。"""
    if not ext_available():
        return None
    import random
    pairs = _pairs()
    if not pairs:
        return None
    if _router is not None:
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
            # 与 tdx_exhq.connect_exhq 一致的参数，保留 heartbeat / auto_retry 语义
            api = TdxExHq_API(heartbeat=True, auto_retry=True)
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


# =========================================================
# 代码桥接：8 位 watchlist code -> (market, numeric_id)
# =========================================================
_EXHQ_CODE_MAP = {}        # 8 位代码 -> (market, numeric_id)
_EXHQ_CODE_MAP_TS = 0.0
_EXHQ_CODE_MAP_TTL = 3600.0
_EXHQ_CODE_MAP_LOCK = threading.Lock()
_EXHQ_CODE_MAP_REFRESHING = False   # 后台刷新进行中标记


def _refresh_code_map():
    """同步刷新合约映射（get_option_codes 枚举可能较慢，仅在后台线程调用）。"""
    global _EXHQ_CODE_MAP, _EXHQ_CODE_MAP_TS
    if not _EXHQ_OK or get_option_codes is None:
        return
    try:
        # 按健康度排序后传入：枚举较慢，优先走延迟低的节点
        rows = get_option_codes(markets={8, 9}, chinese=True,
                                servers=_ordered_triples() or None)
    except Exception:
        return
    if not rows:
        return
    mp = {}
    for market, numeric, _contract_code, _cn_name in rows:
        if numeric:
            mp[str(numeric)] = (market, str(numeric))
    if mp:
        with _EXHQ_CODE_MAP_LOCK:
            _EXHQ_CODE_MAP = mp
            _EXHQ_CODE_MAP_TS = time.time()


def _refresh_code_map_bg():
    global _EXHQ_CODE_MAP_REFRESHING
    try:
        _refresh_code_map()
    finally:
        _EXHQ_CODE_MAP_REFRESHING = False


def _maybe_refresh_async():
    """映射缺失/过期时在后台线程刷新，绝不阻塞当前请求路径。"""
    global _EXHQ_CODE_MAP_REFRESHING
    if not _EXHQ_OK or get_option_codes is None:
        return
    if _EXHQ_CODE_MAP_REFRESHING:
        return
    if time.time() - _EXHQ_CODE_MAP_TS <= _EXHQ_CODE_MAP_TTL:
        return
    _EXHQ_CODE_MAP_REFRESHING = True
    try:
        threading.Thread(target=_refresh_code_map_bg, daemon=True).start()
    except Exception:
        _EXHQ_CODE_MAP_REFRESHING = False


def resolve_exhq_code(code):
    """8 位 watchlist 代码 -> (market, numeric_id) 或 None。优先用映射；缺失时按前缀兜底并后台异步补全映射，绝不阻塞。"""
    code = str(code).strip()
    if not code:
        return None
    with _EXHQ_CODE_MAP_LOCK:
        hit = _EXHQ_CODE_MAP.get(code)
    if hit:
        return hit
    # 兜底：按前缀推定市场（SSE=8 / SZSE=9），code 即数字合约 ID。
    if code[0] == "1":
        fb = (8, code)
    elif code[0] == "9":
        fb = (9, code)
    else:
        return None
    _maybe_refresh_async()
    return fb


# =========================================================
# 归一化接口（board-rt 数据契约）
# =========================================================
def exquote_minute(code):
    """当日分时（1 分钟）：ExHq get_minute_time_data。"""
    res = resolve_exhq_code(code)
    if not res:
        return []
    market, num = res

    def fn(api):
        rows = api.get_minute_time_data(market, num)
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

    return _fetch_coalesced("exq_minute:%s" % code, lambda: _safe_query(fn)) or []


def exquote_quote(code):
    """五档买卖盘口：ExHq get_instrument_quote。"""
    res = resolve_exhq_code(code)
    if not res:
        return {}
    market, num = res

    def fn(api):
        q = api.get_instrument_quote(market, num)
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
            "servertime": time.strftime("%H:%M:%S"),
            "bids": bids,
            "asks": asks,
        }

    return _fetch_coalesced("exq_quote:%s" % code, lambda: _safe_query(fn)) or {}


def exquote_tick(code, count=40):
    """逐笔成交：ExHq get_transaction_data（取最近 count 笔）。"""
    res = resolve_exhq_code(code)
    if not res:
        return []
    market, num = res

    def fn(api):
        rows = api.get_transaction_data(market, num, start=0, count=1800)
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
                    # ExHq get_transaction_data 价格单位为 1/10000 元（分时/盘口为 元），需还原为元
                    "price": float(d["price"]) / 10000.0,
                    "vol": int(d.get("volume") or 0),
                    "num": 1,
                    "buyorsell": bs,
                })
            except Exception:
                continue
        return out

    return _fetch_coalesced("exq_tick:%s" % code, lambda: _safe_query(fn)) or []
