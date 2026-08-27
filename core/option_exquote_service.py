# -*- coding: utf-8 -*-
"""期权扩展行情(ExHq)盘口 / 分时 / 逐笔服务。

数据源（pip 包，已在环境中安装，无需拷贝源码）：
  - pytdx_patches ：对 pytdx 的分时 / 历史K线 / 心跳等接口打补丁（国信、国元专有扩展行情）。
  - tdx_exhq      ：封装 TdxExHq_API 连接与期权合约枚举（EXHQ_SERVERS 含国信7721 / 国元7721 / 通用7727）。

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

# 必须先 import pytdx_patches 以应用补丁，再 import tdx_exhq（其 TdxExHq_API 复用被补丁过的解析器）。
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
        rows = get_option_codes(markets={8, 9}, chinese=True)
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

    return _safe_query(fn) or []


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

    return _safe_query(fn) or {}


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

    return _safe_query(fn) or []
