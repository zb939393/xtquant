# -*- coding: utf-8 -*-
"""xtquant 本地行情（QMT/iQuant）可转债字段补充服务。

双路回退策略（任何一路通就返回数据）：
  路径 A — HTTP 桥 58080（iQuant 策略里跑 qmt_http_bridge_server.py v2）
    POST /cb_detail_extra → 在 iQuant 内部 Python ContextInfo 里调用 get_instrumentdetail
    / get_instrument_detail / bond-rating-related-methods，拍平后返回 { code: {字段} }
  路径 B — 58600 Formulaserver 独立 BSON-RPC 客户端（不依赖 qmt_api 包，纯 socket+bson）
    getInstrumentDetail(code)：基础字段

设计：只在 akshare 侧字段缺失时调用做"补"，不做覆盖；带内存缓存 + TTL。
"""
import os, time, threading, socket, struct, zlib, json
import logging

try:
    import bson  # pymongo 提供
except Exception:
    bson = None

try:
    import requests
except Exception:
    requests = None

from config import Config

log = logging.getLogger(__name__)

_cache = {}
_cache_lock = threading.Lock()
CACHE_KEY_PREFIX = "qmt_cb_detail_v1_"


def _cache_get(key, ttl):
    now = time.time()
    with _cache_lock:
        h = _cache.get(CACHE_KEY_PREFIX + key)
        if h and now - h[0] < ttl:
            return h[1]
    return None


def _cache_put(key, ttl, val):
    with _cache_lock:
        _cache[CACHE_KEY_PREFIX + key] = (time.time(), val)


# ============== 路径 A：HTTP 桥 ==============
def _try_bridge(codes, timeout=10):
    if not Config.BRIDGE_URL or requests is None:
        return False, "disabled or requests not installed"
    url = Config.BRIDGE_URL.rstrip("/") + "/cb_detail_extra"
    headers = {"X-API-Token": Config.BRIDGE_TOKEN, "Content-Type": "application/json"}
    payload = {"codes": list(codes)}
    try:
        t0 = time.time()
        sess = requests.Session()
        resp = sess.post(url, json=payload, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            return False, "HTTP %d: %s" % (resp.status_code, resp.text[:200])
        j = resp.json()
        if not j.get("ok"):
            return False, "bridge err: %s" % j.get("error")
        data = j.get("data") or {}
        out = {}
        for c, d in data.items():
            if isinstance(d, dict):
                out[c] = _clean_qmt_dict(d)
            else:
                out[c] = {}
        log.info("bridge /cb_detail_extra OK: %d codes, %.0fms",
                 len(out), (time.time() - t0) * 1000)
        return True, out
    except Exception as e:
        return False, "exception: %s: %s" % (type(e).__name__, e)


# ============== 路径 B：58600 Formulaserver 独立 BSON-RPC ==============
_NET_CMD_RPC = 3


class _QmtRpc58600:
    def __init__(self, host="127.0.0.1", port=58600, timeout=12):
        self.host = host; self.port = port; self.timeout = timeout
        self._s = None; self.seq = 0
        self._connect()

    def _connect(self):
        self._s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try: self._s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except: pass
        try: self._s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except: pass
        self._s.connect((self.host, self.port))
        if self.timeout > 0: self._s.settimeout(self.timeout)
        self.seq = 0

    @staticmethod
    def _encode(seq, cmd, data):
        bdata = bson.BSON.encode(data)
        pack_len = len(bdata) + 12
        int_seq = int(seq) & 0xFFFFFFFF
        flag_seq = ((int(seq) >> 32) & 0x0F) << 8
        tag = flag_seq
        return struct.pack("!IIHH%ds" % len(bdata), pack_len, int_seq, cmd, tag, bdata)

    def _recv_all(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._s.recv(n - len(buf))
            if not chunk: raise ConnectionError("58600 socket closed")
            buf += chunk
        return buf

    def _recv_one(self):
        hdr = self._recv_all(4)
        pack_len, = struct.unpack_from("!I", hdr, 0)
        rest = self._recv_all(pack_len - 4)
        raw = hdr + rest
        seq, cmd, tag = struct.unpack_from("!IHH", raw, 4)
        body_len = pack_len - 12
        body = raw[12: 12 + body_len]
        compress = tag & 7
        if compress == 1 or compress == 2:
            body = zlib.decompress(body)
        if cmd == _NET_CMD_RPC:
            seq = ((tag >> 8) & 0x0F) << 32 | seq
            data = bson.BSON(body).decode()
        else:
            data = {"_raw": body}
        return seq, cmd, data

    def rpc(self, func, params):
        if self._s is None: self._connect()
        self.seq += 1
        payload = {"func": func, "params": params}
        send_bytes = self._encode(self.seq, _NET_CMD_RPC, payload)
        try:
            self._s.sendall(send_bytes)
        except (BrokenPipeError, ConnectionError):
            try: self._s.close()
            except: pass
            self._connect(); self.seq += 1
            send_bytes = self._encode(self.seq, _NET_CMD_RPC, payload)
            self._s.sendall(send_bytes)
        while True:
            seq, cmd, data = self._recv_one()
            if seq == self.seq:
                if data.get("status") == 0: return data.get("params")
                raise RuntimeError("rpc %s err: %s" % (func, data.get("params")))

    def get_instrument_detail(self, code):
        resp = self.rpc("getInstrumentDetail", {"strOptionCode": code})
        result = resp.get("result", []) if resp else []
        if isinstance(result, list) and len(result) > 0 and isinstance(result[0], dict):
            return result[0]
        if isinstance(result, dict): return result
        return {}

    def close(self):
        try: self._s.close()
        except: pass
        self._s = None


_rpc58600_ref = {"client": None, "lock": threading.Lock()}


def _get_rpc58600():
    with _rpc58600_ref["lock"]:
        if _rpc58600_ref["client"] is None:
            if bson is None:
                raise RuntimeError("pymongo(bson) 未安装，无法走 58600 RPC；请 pip install pymongo")
            c = _QmtRpc58600("127.0.0.1", 58600, timeout=8)
            _rpc58600_ref["client"] = c
        return _rpc58600_ref["client"]


def _try_rpc58600(codes):
    t0 = time.time()
    out = {}
    try:
        cli = _get_rpc58600()
    except Exception as e:
        return False, "58600 connect fail: %s: %s" % (type(e).__name__, e)
    try:
        for c in codes:
            try:
                d = cli.get_instrument_detail(c)
            except Exception as e:
                log.debug("58600 getInstrumentDetail %s fail: %s", c, e)
                out[c] = {}
                continue
            if isinstance(d, dict):
                out[c] = _clean_qmt_dict(d)
            else:
                out[c] = {}
        log.info("rpc58600 OK: %d codes, %.0fms",
                 len(out), (time.time() - t0) * 1000)
        return True, out
    except Exception as e:
        return False, "exception: %s: %s" % (type(e).__name__, e)


# ============== 公用：清洗 + 映射到 akshare 标准列 ==============
def _clean_val(v):
    if isinstance(v, float) and (v > 1e300 or v < -1e300): return None
    if isinstance(v, bool): return v
    if isinstance(v, int) and v >= 20700000: return None
    if isinstance(v, str) and v.strip() == "": return None
    return v


def _clean_qmt_dict(d):
    out = {}
    ei = d.get("ExtendInfo")
    if isinstance(ei, dict):
        for k, v in ei.items():
            cv = _clean_val(v)
            if cv is not None: out["EI_" + k] = cv
    for k, v in d.items():
        if k == "ExtendInfo": continue
        cv = _clean_val(v)
        if cv is not None: out[k] = cv
    return out


QMT_TO_STD = {
    # 基础标识
    "InstrumentName":       "bond_name_qmt",
    "InstrumentID":         "instrument_id_qmt",
    "ExchangeID":           "exchange_id_qmt",
    "Abbreviation":         "bond_name_abbr_qmt",
    "ProductName":          "product_name_qmt",
    # 行情辅助
    "PreClose":             "pre_close_qmt",
    "SettlementPrice":      "settlement_price_qmt",
    "UpStopPrice":          "up_stop_price_qmt",
    "DownStopPrice":        "down_stop_price_qmt",
    "PriceTick":            "price_tick_qmt",
    "VolumeMultiple":       "volume_multiple_qmt",
    # 规模 / 日期
    "TotalVolumn":          "total_vol_zhang_qmt",
    "FloatVolumn":          "float_vol_zhang_qmt",
    "OpenDate":             "list_date_qmt",
    "CreateDate":           "create_date_qmt",
    "ExpireDate":           "maturity_date_qmt",
    "AccumulatedInterest":  "accrued_interest_qmt",
    # 可转债专用（ContextInfo 侧可能提供）
    "CreditRating":         "rating",
    "DebtRating":           "rating_alt",
    "CreditRatingComp":     "rating_agency",
    "RatingComp":           "rating_agency_alt",
    "IssuerRating":         "issuer_rating",
    "CouponRate":           "coupon_pct",
    "Coupon":               "coupon_pct_alt",
    "CouponInterestStartDate": "coupon_start_date_qmt",
    "IssuePrice":           "issue_price_qmt",
    "MaturityDate":         "maturity_date",
    "MaturityPrice":        "maturity_price",
    "RedemptionPrice":      "redemption_price_qmt",
    "CallPrice":            "call_trigger_price",
    "PutPrice":             "put_trigger_price",
    "ConversionPrice":      "conv_price",
    "FirstConversionDate":  "conv_start_date",
    "LastConversionDate":   "conv_end_date",
    "RedemptionDate":       "redemption_date_qmt",
    "PutDate":              "put_start_date_qmt",
    "CallDate":             "call_date_qmt",
    "UnderlyingCode":       "stock_code_qmt",
    "UnderlyingStockCode":  "stock_code_qmt2",
    "UnderlyingName":       "stock_name_qmt",
}


def to_std_row(qmt_row):
    out = {}
    for qk, qv in qmt_row.items():
        if qv is None: continue
        std = QMT_TO_STD.get(qk)
        if std is None: continue
        if std in ("rating", "rating_alt", "issuer_rating", "rating_agency", "rating_agency_alt") \
                and isinstance(qv, str):
            s = qv.strip()
            if s == "" or len(s) > 20: continue
            out[std] = s; continue
        if std.endswith("_date_qmt") or std.endswith("_date"):
            if isinstance(qv, int) and 19900000 <= qv < 21000000:
                s = str(qv)
                out[std] = "%s-%s-%s" % (s[:4], s[4:6], s[6:8])
                continue
        if std in ("coupon_pct", "coupon_pct_alt") and isinstance(qv, (int, float)):
            if -1.0 <= qv <= 20.0:
                out[std] = float(qv); continue
        if std in ("conv_price", "maturity_price", "call_trigger_price",
                   "put_trigger_price", "redemption_price_qmt",
                   "pre_close_qmt", "issue_price_qmt") \
                and isinstance(qv, (int, float)):
            if 1.0 <= qv <= 2000.0:
                out[std] = float(qv); continue
        if std in ("total_vol_zhang_qmt", "float_vol_zhang_qmt") \
                and isinstance(qv, (int, float)):
            yi = qv / 1e6
            std_yi = std.replace("_zhang_qmt", "_yi_qmt")
            out[std_yi] = round(yi, 6)
            out[std] = float(qv)
            continue
        out[std] = qv
    return out


# ============== 对外主入口 ==============
def augment_codes(codes, ttl=None):
    t0 = time.time()
    ttl = ttl or Config.AK_CACHE_TTL
    codes = list(codes) if not isinstance(codes, list) else codes
    codes = [str(c) for c in codes if c]
    if not codes:
        return {"__meta__": {"via": "none", "elapsed_ms": 0, "reason": "empty codes"}}

    cache_key = "|".join(sorted(codes))
    cached = _cache_get(cache_key, ttl)
    if cached is not None:
        log.debug("augment_codes cache hit: %d codes", len(codes))
        return cached

    bridge_ok, bridge_map = _try_bridge(codes)
    rpc58600_ok, rpc58600_map = False, {}
    if not bridge_ok or any(not v for v in bridge_map.values()):
        need_rpc_codes = [c for c in codes
                          if (not bridge_ok or not bridge_map.get(c))]
        if need_rpc_codes and bson is not None:
            rpc58600_ok, rpc58600_map = _try_rpc58600(need_rpc_codes)

    result = {}
    hits_cnt = {}
    for c in codes:
        if bridge_ok and isinstance(bridge_map, dict) and bridge_map.get(c):
            raw = bridge_map.get(c)
        elif isinstance(rpc58600_map, dict):
            raw = rpc58600_map.get(c, {})
        else:
            raw = {}
        if not isinstance(raw, dict) or not raw:
            result[c] = {}; hits_cnt[c] = 0; continue
        std_row = to_std_row(raw)
        std_row["__qmt_raw_keys"] = sorted(raw.keys())
        result[c] = std_row
        hit_n = sum(1 for k, v in std_row.items()
                    if not k.startswith("__")
                    and (v not in (None, "", [], {})))
        hits_cnt[c] = hit_n

    if bridge_ok and rpc58600_ok: via = "mixed"
    elif bridge_ok: via = "bridge"
    elif rpc58600_ok: via = "rpc58600"
    else: via = "none"

    meta = {
        "via": via,
        "bridge_ok": bridge_ok,
        "bridge_msg": "" if bridge_ok else str(bridge_map)[:200],
        "rpc58600_ok": rpc58600_ok,
        "rpc58600_msg": "" if rpc58600_ok else str(rpc58600_map)[:200],
        "elapsed_ms": int((time.time() - t0) * 1000),
        "field_hits_per_code": hits_cnt,
    }
    result["__meta__"] = meta
    _cache_put(cache_key, ttl, result)
    return result


def probe():
    out = {}
    if Config.BRIDGE_URL:
        ok, msg_or_map = _try_bridge(["110075.SH"], timeout=4)
        out["bridge"] = {"ok": ok,
                         "msg": (msg_or_map if isinstance(msg_or_map, str) else "ok")}
    else:
        out["bridge"] = {"ok": False, "msg": "bridge URL not configured"}
    if bson is not None:
        ok, msg_or_map = _try_rpc58600(["110075.SH"])
        out["rpc58600"] = {"ok": ok,
                           "msg": (msg_or_map if isinstance(msg_or_map, str) else "ok")}
    else:
        out["rpc58600"] = {"ok": False, "msg": "bson(pymongo) not installed"}
    return out


if __name__ == "__main__":
    import pprint
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print("=== probe channels ===")
    pprint.pprint(probe())
    print("\n=== augment_codes([110075.SH, 110081.SH]) ===")
    r = augment_codes(["110075.SH", "110081.SH"], ttl=1)
    meta = r.pop("__meta__")
    print("meta:", json.dumps(meta, ensure_ascii=False, indent=2))
    for c, row in r.items():
        print("\n%s: %d std columns" % (c, len(row)))
        for k, v in row.items():
            print("  %s = %s" % (k, v))
