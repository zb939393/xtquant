# -*- coding: utf-8 -*-
"""akshare 数据服务封装：行情快照 / K线 / 可转债全指标。"""
import os
import logging
import time, threading
import concurrent.futures as _futures
# akshare 拉东财/集思录时若环境设了 HTTPS_PROXY 会被代理拦掉，先清掉
for _k in ("HTTP_PROXY","HTTPS_PROXY","http_proxy","https_proxy"):
    os.environ.pop(_k, None)
import math, datetime as dt
import re
import pandas as pd
import akshare as ak
from config import Config
# PyTDX（通达信原生协议，3秒轮询不封IP）
try:
    import pytdx
    from pytdx.hq import TdxHq_API as _TdxHq
    import concurrent.futures as _futures
    _PYTDX_OK = True
except Exception as _pytdx_import_e:
    _PYTDX_OK = False
    _pytdx_import_e

_cache = {}
_lock = threading.Lock()

# 票面利率文本解析：抽所有 "X年Y%" 里的 Y（float），同时抽"到期赎回价XXX元"
_COUPON_RE_FLOAT_PCT = re.compile(
    r"(\d+(?:\.\d+)?)\s*[%％]")  # 找形如 0.30% 的数字
_REDEEM_RE = re.compile(
    r"到期赎回[价格为是]+\s*(\d+(?:\.\d+)?)\s*(?:元|块)?"
    r"|到期[赎回]\s*[^\d]*(\d+(?:\.\d+)?)\s*元")
# RATING 去尾噪：常见尾部噪声字符（比如 AA+sti → AA+）
_RATING_CLEAN_RE = re.compile(r"^(.+?)(?:sti|shi|zh|jiao|[补修调])*$", re.IGNORECASE)


def _parse_coupon_text(text):
    """解析 INTEREST_RATE_EXPLAIN 之类的长文本。

    返回 dict:
      { "coupon_rates": [0.3, 0.5, ...],    # 各年百分比
        "coupon_avg": 1.05,                  # 平均
        "coupon_pct": 0.3,                   # 第一年或当前最近的票面
        "coupon_year_cnt": 6,                # 年数
        "maturity_price_from_text": 108.0,   # 文本里提及的到期赎回价（元，可 None）
        "coupon_text_norm": text 归一化后。 }
    """
    empty = {"coupon_rates": [], "coupon_avg": None, "coupon_pct": None,
             "coupon_year_cnt": 0, "maturity_price_from_text": None,
             "coupon_text_norm": ""}
    if not text:
        return empty
    s = str(text)
    empty["coupon_text_norm"] = s.strip()

    # 1) 抽所有百分比数字
    nums = [float(x) for x in _COUPON_RE_FLOAT_PCT.findall(s)]
    # 过滤：可转债票面利率一般在 0~10 之间（别把强赎130%、回售70%这些给抽进来）
    rates = [n for n in nums if 0 <= n <= 10]
    empty["coupon_rates"] = rates
    empty["coupon_year_cnt"] = len(rates)
    if rates:
        empty["coupon_pct"] = rates[0]  # 第一年票面
        empty["coupon_avg"] = round(sum(rates) / len(rates), 4)

    # 2) 抽到期赎回价（maturity_price）
    for m in _REDEEM_RE.finditer(s):
        a, b = m.groups()
        v = a or b
        try:
            f = float(v)
        except Exception:
            continue
        if 100 <= f <= 200:
            empty["maturity_price_from_text"] = f
            break
    return empty


def _clean_rating(r):
    if r is None:
        return ""
    s = str(r).strip()
    if not s:
        return ""
    # 常见 AAA AA+ AA AA- A+ A BBB+ BBB BBB- 格式；如果尾部挂了非字母+± 的杂字符去掉
    if len(s) > 2 and s[-1] not in "+-":
        # 保留首 2-5 个 "A/B/C/D ± +" 字符
        m = re.match(r"^([ABCDabcd]{1,4}[+\-\+\-]?[+\-\+\-]?)", s)
        if m:
            return m.group(1)
    return s



# ============================================================================
# 方案 B 主路径（用户要求"东财一次性全部拉取"）：datacenter-web RPT_BOND_CB_LIST
#   1050 只 × 72 列，分 3 页（pz=500），3 秒拉完，直接提供 list_date / maturity_date /
#   RATING(债项评级) / INTEREST_RATE_EXPLAIN / EXECUTE_PRICE_SH /
#   REDEEM_TRIG_PRICE / RESALE_TRIG_PRICE / PARTY_NAME(评级公司) /
#   INITIAL_TRANSFER_PRICE / TRANSFER_START_DATE / TRANSFER_END_DATE 等
# 如果一次性接口失败，回退到 ths 一次性 + cov_info 单只慢拉的两路兜底。
# ============================================================================
def _cb_fetch_rpt_bond_cb_list(max_age=3600, fail_ttl=300):
    """东财 datacenter-web RPT_BOND_CB_LIST 一次性全表分页抓取。
    返回: (成功) { 6位代码: {std列字典}, ... }
          (失败或数据太少) {}  （此时调用方应 fallback 到 ths+cov_info）
    """
    cache_key_full = "cb_rpt_bond_cb_list_v1"
    cache_key_fail = "cb_rpt_bond_cb_list_fail_v1"

    # 1) 失败冷却：fail_ttl 内不再重复打
    fail_mark = _cache_get_inner(cache_key_fail, fail_ttl)
    if fail_mark:
        return {}

    # 2) 成功缓存：长 TTL（评级/票息/上市日/到期日=半静态）
    cached = _cache_get_inner(cache_key_full, max_age)
    if isinstance(cached, dict) and len(cached) >= 800:
        return cached

    log = logging.getLogger(__name__)
    try:
        import urllib.parse
        import requests as _reqs
    except Exception as imp_e:
        log.warning("requests 不可用，RPT一次性表跳过：%s", imp_e)
        _cache_put_inner(cache_key_fail, fail_ttl, {"t": time.time(), "err": "no_requests"})
        return {}

    sess = _reqs.Session()
    sess.trust_env = False  # ⚠️ 禁用代理（否则 HTTPS_PROXY 会误走代理报 RemoteDisconnected）
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/127.0 Safari/537.36",
        "Referer": "https://data.eastmoney.com/",
        "Accept": "*/*",
    })

    TS = str(int(time.time()*1000))
    base = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    common_params = dict(
        sortColumns="SECURITY_CODE", sortTypes="1",
        reportName="RPT_BOND_CB_LIST", columns="ALL",
        source="WEB", client="WEB", _=TS,
    )

    # 3) 先第 1 页拿 total，决定页数
    all_rows = []
    pages_done = 0
    pz = 500
    try:
        p1 = dict(common_params, pageSize=str(pz), pageNumber="1")
        url1 = base + "?" + urllib.parse.urlencode(p1)
        t0 = time.time()
        r1 = sess.get(url1, timeout=20)
        j1 = r1.json()
        if not j1.get("success"):
            log.warning("[RPT_BOND_CB_LIST] 第1页失败: %s", (j1.get("message") or "")[:120])
            _cache_put_inner(cache_key_fail, fail_ttl, {"t": time.time(), "err": j1.get("message") or "fail"})
            return {}
        res = j1.get("result") or {}
        total = int(res.get("count") or 0)
        data1 = res.get("data") or []
        all_rows.extend(data1)
        pages_done = 1
        pages_total = max(1, (total + pz - 1) // pz)
        log.info("[RPT_BOND_CB_LIST] 第1/%d页 rows=%d total=%d t=%.1fs",
                 pages_total, len(data1), total, time.time() - t0)
        time.sleep(0.5)

        # 4) 后续页
        for pn in range(2, pages_total + 1):
            pp = dict(common_params, pageSize=str(pz), pageNumber=str(pn))
            url = base + "?" + urllib.parse.urlencode(pp)
            t2 = time.time()
            r = sess.get(url, timeout=20)
            j = r.json()
            data = j.get("result", {}).get("data") or []
            all_rows.extend(data)
            log.info("[RPT_BOND_CB_LIST] 第%d/%d页 rows=%d t=%.1fs",
                     pn, pages_total, len(data), time.time() - t2)
            time.sleep(0.4)
    except Exception as net_e:
        log.warning("[RPT_BOND_CB_LIST] 分页拉取异常(第%d页后)：%s", pages_done, net_e)
        if len(all_rows) < 500:  # 只拿到 1 页以内，数据量太小视为失败
            _cache_put_inner(cache_key_fail, fail_ttl, {"t": time.time(), "err": repr(net_e)})
            return {}

    if len(all_rows) < 800:
        log.warning("[RPT_BOND_CB_LIST] 总行数 %d < 800，判定失败 fallback 原方案", len(all_rows))
        _cache_put_inner(cache_key_fail, fail_ttl, {"t": time.time(), "err": "too_few:%d" % len(all_rows)})
        return {}

    # 5) 按 6 位 code 拍平标准列（完全复用 cov_info 的列映射，保证下游 merge 代码一致）
    result = {}
    rating_hit = 0
    for row in all_rows:
        c6 = str(row.get("SECURITY_CODE") or "").strip()
        if len(c6) != 6 or not c6.isdigit():
            continue
        rating = _clean_rating(row.get("RATING"))
        if rating:
            rating_hit += 1
        coupon_info = _parse_coupon_text(row.get("INTEREST_RATE_EXPLAIN") or "")
        # 到期赎回价：文本里提到 > EXECUTE_PRICE_SH > EXECUTE_PRICE_HS
        mp = coupon_info.get("maturity_price_from_text")
        if mp is None:
            for k in ("EXECUTE_PRICE_SH", "EXECUTE_PRICE_HS"):
                try: v = float(row.get(k))
                except: v = None
                if v is not None and 100 <= v <= 200:
                    mp = v; break
        listing = _date_norm(row.get("LISTING_DATE"))
        expire = _date_norm(row.get("EXPIRE_DATE"))
        conv_start = _date_norm(row.get("TRANSFER_START_DATE"))
        conv_end = _date_norm(row.get("TRANSFER_END_DATE"))
        public_dt = _date_norm(row.get("PUBLIC_START_DATE"))
        redeem_trig = row.get("REDEEM_TRIG_PRICE")
        try: redeem_trig = float(redeem_trig) if redeem_trig not in (None, "") else None
        except: redeem_trig = None
        resale_trig = row.get("RESALE_TRIG_PRICE")
        try: resale_trig = float(resale_trig) if resale_trig not in (None, "") else None
        except: resale_trig = None
        conv_price_init = row.get("INITIAL_TRANSFER_PRICE")
        try: conv_price_init = float(conv_price_init) if conv_price_init not in (None, "") else None
        except: conv_price_init = None
        coupon_ir = row.get("COUPON_IR")
        try: coupon_ir = float(coupon_ir) if coupon_ir not in (None, "") else None
        except: coupon_ir = None
        result[c6] = {
            "rating": rating,
            "coupon_pct": coupon_info.get("coupon_avg") or coupon_info.get("coupon_pct") or coupon_ir,
            "coupon_first_pct": coupon_info.get("coupon_pct"),
            "coupon_avg_pct": coupon_info.get("coupon_avg"),
            "coupon_year_cnt": coupon_info.get("coupon_year_cnt"),
            "coupon_rates_json": ",".join(str(x) for x in coupon_info.get("coupon_rates", [])),
            "coupon_text": coupon_info.get("coupon_text_norm"),
            "maturity_price": mp,
            "list_date": listing,
            "maturity_date": expire,
            "convert_start_date": conv_start,
            "convert_end_date": conv_end,
            "issue_date": public_dt,
            "redeem_trig_price_rpt": redeem_trig,
            "resale_trig_price_rpt": resale_trig,
            "initial_conv_price_rpt": conv_price_init,
            "issue_size_yi_cov": row.get("ACTUAL_ISSUE_SCALE"),
            "stock_code_cov": row.get("CONVERT_STOCK_CODE"),
            "rating_agency": str(row.get("PARTY_NAME") or "").strip(),  # 评级公司
            "coupon_ir_raw": coupon_ir,
            "param_name": str(row.get("PARAM_NAME") or "").strip(),
            "bond_full_name_rpt": str(row.get("SECURITY_NAME_ABBR") or "").strip(),
            "delist_date_rpt": _date_norm(row.get("DELIST_DATE")),
        }

    log.info("[RPT_BOND_CB_LIST] 一次全表完成：%d 只，债项评级非空 %d 只 (%.0f%%)，"
             "list_date 非空 %d 只，maturity_date 非空 %d 只",
             len(result), rating_hit,
             (100*rating_hit/len(result)) if result else 0,
             sum(1 for v in result.values() if v.get("list_date")),
             sum(1 for v in result.values() if v.get("maturity_date")))

    _cache_put_inner(cache_key_full, max_age, result)
    return result


def _cb_merge_rpt_bond_cb_list(df: pd.DataFrame):
    """把 RPT_BOND_CB_LIST 一次性结果按行序对齐合并（同 _cb_merge_cov_info 保守原则）。

    返回 (df, matched_count)：matched_count<300 调用方需 fallback 原方案B。
    """
    df = df.copy()
    N = len(df)
    if N == 0:
        return df, 0
    rpt = _cb_fetch_rpt_bond_cb_list()
    if not rpt:
        return df, 0

    c6_all = (
        df["bond_code"].astype(str).str.extract(r"(\d{6})", expand=False).fillna("").tolist()
        if "bond_code" in df.columns else [""] * N
    )
    per_row = [rpt.get(c6, {}) if c6 else {} for c6 in c6_all]
    matched = sum(1 for x in per_row if x)

    CORE = ["rating", "coupon_pct", "maturity_price", "list_date", "maturity_date"]

    def _empty(a):
        if a is None: return True
        if isinstance(a, float) and pd.isna(a): return True
        if isinstance(a, str) and a.strip() in ("", "-", "--", "\u2014"): return True
        return False

    # 追加列：非 CORE 键直接挂列（存在则 __rpt 后缀）
    append_keys = set()
    for r in per_row:
        for k in r.keys():
            if k not in CORE:
                append_keys.add(k)
    append_keys = sorted(append_keys)
    for k in append_keys:
        vals = [r.get(k) for r in per_row]
        if k in df.columns:
            df[k + "__rpt"] = vals
        else:
            df[k] = vals

    # 核心列：ak 空才填
    for col in CORE:
        cvals = [r.get(col) for r in per_row]
        if col not in df.columns:
            df[col] = cvals
            continue
        arr = df[col].values.copy()
        for i in range(N):
            if not _empty(arr[i]):
                continue
            v = cvals[i]
            if _empty(v):
                continue
            try: arr[i] = v
            except (ValueError, TypeError): pass
        df[col] = arr

    return df, matched


# ============================================================================
# 方案 B 兜底路径（ths 一次性 + cov_info 单只慢拉）
# ============================================================================

# ============================================================================
# 方案 B 兜底路径（ths 一次性 + cov_info 单只慢拉）
# =============================================================================
def _cb_merge_ths_info(df: pd.DataFrame) -> pd.DataFrame:
    """用 bond_zh_cov_info_ths（956 只全量）填 list_date / maturity_date。

    输入 df 必须已经有 "bond_code" 列（形如 113697.SH）。
    输出 df 多出来 ths_* 原始列，后面 apply_std_columns 会自动 coalesce。
    """
    try:
        df_ths = _cached("cb_ths_info", Config.AK_CACHE_TTL,
                         lambda: ak.bond_zh_cov_info_ths())
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("ths_info 拉取失败：%s", e)
        return df
    df_ths = df_ths.copy()
    # 统一 6 位 code
    df_ths["_6code"] = df_ths["债券代码"].astype(str).str.extract(r"(\d{6})", expand=False)
    df["_6code"] = df["bond_code"].astype(str).str.extract(r"(\d{6})", expand=False)
    cols_want = ["_6code",
                 "上市日期",   # → list_date
                 "到期时间",   # → maturity_date
                 "债券简称",   # → bond_name 交叉验证
                 "正股代码",   # → stock_code
                 "正股简称",   # → stock_name
                 "转股价格",   # → conv_price (ths)
                 "实际发行量"]  # → issue_size_yi
    have_cols = [c for c in cols_want if c in df_ths.columns]
    df_ths_small = df_ths[have_cols]
    df = df.merge(df_ths_small, on="_6code", how="left", suffixes=("", "_ths"))
    # apply_std_columns 后面会按别名合这些列（只要别名里有就行）
    return df


def _cb_fetch_cov_info(codes_6_list, ttl=None):
    """批量拉 bond_zh_cov_info(symbol, '基本信息')。

    缓存层次：整批磁盘快取(data/_cov_info_cache.pkl) → 单只内存快取 → 单只磁盘快取(data/_cache_inner/)。
    返回 dict: { 6code: { std_col: val, ... } }。
    """
    import pickle as _pkl
    ttl = ttl or Config.AK_CACHE_TTL
    import time as _tt
    _now = _tt.time()
    t0 = _now  # 计时基准和快照时间一致就行
    codes_uniq = list(dict.fromkeys(codes_6_list))
    result = {}  # 必须在任何条件赋值前声明 → 避免 Python 把 result 视为条件局部变量
    _full_path = os.path.join(Config.DATA_DIR, "_cov_info_cache.pkl")
    try:
        if os.path.exists(_full_path):
            with open(_full_path, "rb") as _ff:
                _snap = _pkl.load(_ff)
            _ts = _snap.get("ts", 0)
            _dat = _snap.get("data") or {}
            _ttl_full = max(ttl, 3600)  # 半静态字段（rating/coupon/上市日/到期日）：单独 1 小时磁盘快取
            if _now - _ts < _ttl_full:
                # 预填充：只要命中率 ≥ 50%（避免小批次无效）就用磁盘快取打底，剩余缺失再逐只网络拉
                _pre = {c: _dat[c] for c in codes_uniq if c and c in _dat}
                _total = max(1, sum(1 for c in codes_uniq if c))
                if len(_pre) / _total >= 0.5:
                    logging.getLogger(__name__).info(
                        "bond_cov_info 整批磁盘快取打底：%d/%d 命中（%.0f%%）",
                        len(_pre), _total, 100*len(_pre)/_total)
                    result.update(_pre)
                del _pre, _dat, _ts, _snap
    except Exception as _e:
        logging.getLogger(__name__).warning("读整批磁盘快取失败：%s", _e)

    def _one(sym):
        key = "cb_cov_info_v2_%s" % sym
        hit = _cache_get_inner(key, ttl)
        if hit is not None:
            return hit
        try:
            d = ak.bond_zh_cov_info(symbol=sym, indicator="基本信息")
            if len(d) == 0:
                row = {}
            else:
                row = d.iloc[0].to_dict()
        except Exception as e:
            row = {"__err__": repr(e)}
        _cache_put_inner(key, ttl, row)
        return row

    for sym in codes_uniq:
        if not sym:
            continue
        if sym in result:
            continue  # 整批快取预填充了，跳过
        r = _one(sym)
        if not r or "__err__" in r:
            continue
        # 拍平标准列
        rating = _clean_rating(r.get("RATING"))
        # 到期赎回价 优先级：文本里提到 > EXECUTE_PRICE_SH（SH=上海）
        coupon_info = _parse_coupon_text(r.get("INTEREST_RATE_EXPLAIN") or "")
        mp = coupon_info.get("maturity_price_from_text")
        for k in ("EXECUTE_PRICE_SH", "EXECUTE_PRICE_HS"):
            if mp is None:
                try: v = float(r.get(k))
                except: v = None
                if v is not None and 100 <= v <= 200:
                    mp = v
        listing = _date_norm(r.get("LISTING_DATE"))
        expire = _date_norm(r.get("EXPIRE_DATE"))
        out = {
            "rating": rating,
            "coupon_pct": coupon_info.get("coupon_avg") or coupon_info.get("coupon_pct"),
            "coupon_first_pct": coupon_info.get("coupon_pct"),
            "coupon_avg_pct": coupon_info.get("coupon_avg"),
            "coupon_year_cnt": coupon_info.get("coupon_year_cnt"),
            "coupon_rates_json": ",".join(str(x) for x in coupon_info.get("coupon_rates", [])),
            "coupon_text": coupon_info.get("coupon_text_norm"),
            "maturity_price": mp,
            "list_date": listing,
            "maturity_date": expire,
            "issue_size_yi_cov": r.get("ACTUAL_ISSUE_SCALE"),
            "stock_code_cov": r.get("CONVERT_STOCK_CODE"),
        }
        result[sym] = out

    logging.getLogger(__name__).info(
        "bond_cov_info 批拉：%d只 耗时%.1fs 成功%d只",
        len(codes_uniq), _tt.time() - t0, len(result))
    # 成功 >= 100 只时，写回整批磁盘快取（避免小批次缓存污染）
    try:
        if len(result) >= 50:
            _full_path2 = os.path.join(Config.DATA_DIR, "_cov_info_cache.pkl")
            os.makedirs(os.path.dirname(_full_path2), exist_ok=True)
            with open(_full_path2, "wb") as _ff:
                _pkl.dump({"ts": _now, "data": result}, _ff, protocol=4)
    except Exception as _e:
        logging.getLogger(__name__).warning("写整批磁盘快取失败：%s", _e)
    return result


# 为了让方案B merge 仍然能用 _cache 锁（独立命名避免和外部 _cached 冲突）
def _cache_inner_disk_path(k):
    import hashlib as _hl
    safe = _hl.md5(k.encode("utf-8")).hexdigest()
    return os.path.join(Config.DATA_DIR, "_cache_inner", safe + ".pkl")

def _cache_get_inner(k, ttl):
    import pickle as _pkl, time as _t
    now = _t.time()
    with _lock:
        h = _cache.get(k)
        if h and now - h[0] < ttl:
            return h[1]
    # 内存 miss → 磁盘
    try:
        p = _cache_inner_disk_path(k)
        if os.path.exists(p):
            mt = os.path.getmtime(p)
            if now - mt < ttl:
                with open(p, "rb") as _ff:
                    val = _pkl.load(_ff)
                with _lock:
                    _cache[k] = (now, val)
                return val
    except Exception:
        pass
    return None

def _cache_put_inner(k, ttl, v):
    import pickle as _pkl, time as _t
    now = _t.time()
    with _lock:
        _cache[k] = (now, v)
    try:
        p = _cache_inner_disk_path(k)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as _ff:
            _pkl.dump(v, _ff, protocol=4)
    except Exception:
        pass

def _date_norm(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v)
    s = s.replace("/","-").replace(".","-").replace(" ","-").split("-")
    parts = [x for x in s if x]
    if len(parts) >= 3:
        return "%s-%s-%s" % (parts[0].rjust(4,"0")[:4],
                             parts[1].rjust(2,"0")[:2],
                             parts[2].rjust(2,"0")[:2])
    # 形如 20210827
    digits = re.sub(r"\D","", s)
    if len(digits) >= 8:
        return "%s-%s-%s" % (digits[:4], digits[4:6], digits[6:8])
    return ""


def _cb_merge_cov_info(df: pd.DataFrame) -> pd.DataFrame:
    """把 bond_zh_cov_info 批拉结果 **按 df 的行序直接对齐合并**。

    - 不按 bond_code 做 merge（易因列重复 / 重复键报错），直接按行号取 cov 返回值；
    - 合并规则：**ak 已有不覆盖**（同 QMT 的保守原则）。
    """
    df = df.copy()
    N = len(df)
    if N == 0:
        return df

    c6_all = (
        df["bond_code"].astype(str).str.extract(r"(\d{6})", expand=False).fillna("").tolist()
        if "bond_code" in df.columns else [""] * N
    )
    cov = _cb_fetch_cov_info([c for c in c6_all if c])

    per_row = [cov.get(c6, {}) if c6 else {} for c6 in c6_all]

    CORE = ["rating", "coupon_pct", "maturity_price", "list_date", "maturity_date"]

    # 追加列：除 CORE 外 cov 返回的键，直接挂新列（若列已存在则加 __cov 后缀）
    append_keys = set()
    for r in per_row:
        for k in r.keys():
            if k not in CORE:
                append_keys.add(k)
    append_keys = sorted(append_keys)
    for k in append_keys:
        vals = [r.get(k) for r in per_row]
        if k in df.columns:
            df[k + "__cov"] = vals
        else:
            df[k] = vals

    def _empty(a):
        if a is None:
            return True
        if isinstance(a, float) and pd.isna(a):
            return True
        if isinstance(a, str) and a.strip() in ("", "-", "--", "\u2014"):
            return True
        return False

    # 核心列：ak 空才填
    for col in CORE:
        cvals = [r.get(col) for r in per_row]
        if col not in df.columns:
            df[col] = cvals
            continue
        arr = df[col].values.copy()
        for i in range(N):
            if not _empty(arr[i]):
                continue
            v = cvals[i]
            if _empty(v):
                continue
            try:
                arr[i] = v
            except (ValueError, TypeError):
                pass
        df[col] = arr
    return df


# =============================================================================
# 方案 B-额外：akshare bond_cov_comparison 拿 主体评级 issuer_rating
# =============================================================================
def _cb_fetch_issuer_rating(max_age=3600, fail_ttl=300):
    """获取主体评级 issuer_rating（目前公开API暂无此字段，保留架构便于未来接入）。

    ⚠️ 2025-04-24 对东财/同花顺/巨潮全部公开一次性/单只接口普查结果：
        - RPT_BOND_CB_LIST: 1050只×72字段，只有债项评级 RATING 单列，无主体评级 ISSUER_RATING。
        - push2行情(bond_cov_comparison): 仅行情列，不含任何评级字段。
        - bond_zh_cov_info / bond_zh_cov_info_ths / bond_cov_issue_cninfo: 均无主体评级。
      因此本函数目前 fast-path 直接返回空，并写短TTL缓存。
      未来东财 RPT_BOND_CB_LIST 升级追加 ISSUER_RATING 列后，只需删除下方 FAST_PATH 块即可启用。

    失败返回空 dict，且仅短 TTL(fail_ttl) 记录失败标记（避免整小时缓存空值占坑）；
    成功写长 TTL(max_age)。主体评级是半静态数据。
    """
    cache_key_full = "cb_issuer_rating_full_v1"
    cache_key_fail = "cb_issuer_rating_fail_v1"

    # ⚡ FAST_PATH: 东财/同花顺/巨潮所有公开API暂无主体评级字段，避免无意义网络请求
    log_fast = logging.getLogger(__name__)
    FAST_EMPTY = {}
    cached_empty = _cache_get_inner(cache_key_full + "_fast", max(3600, max_age))
    if isinstance(cached_empty, dict):
        FAST_EMPTY = cached_empty
    else:
        log_fast.info("[issuer_rating] 所有公开API普查确认暂无主体评级ISSUER_RATING独立字段，fast-path返回空（保留架构待未来东财报表升级）。"
                      "债项评级rating与评级公司rating_agency请参考其他列。")
        _cache_put_inner(cache_key_full + "_fast", max(3600, max_age), FAST_EMPTY)
    return FAST_EMPTY

    # 1) 如果上次失败在短 fail_ttl 内，直接返回空（避免持续触发东财被封）
    fail_mark = _cache_get_inner(cache_key_fail, fail_ttl)
    if fail_mark:
        return {}

    # 2) 查成功缓存
    cached = _cache_get_inner(cache_key_full, max_age)
    if cached is not None and isinstance(cached, dict) and len(cached) > 100:
        return cached

    log = logging.getLogger(__name__)
    try:
        import time as _t, urllib.parse
        import requests as _reqs
        t0 = _t.time()

        # 直接打 push2 接口（和 akshare bond_cov_comparison 同源同参数）→ 关闭代理信任
        sess = _reqs.Session()
        sess.trust_env = False  # ⚠️ 禁用 HTTPS_PROXY/系统代理（否则 RemoteDisconnected 卡 8s+）
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/127 Safari/537.36",
            "Referer": "https://quote.eastmoney.com/",
        })
        TS = str(int(_t.time()*1000))

        def _one_page(pn):
            params = {
                "pn": str(pn), "pz": "500", "po": "1", "np": "1",
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": "2", "invt": "2", "fid": "f243", "fs": "b:MK0354",
                # 拉尽可能多的字段，包含当前未命名的 f 列（主体评级可能就在某列）
                "fields": ",".join([f"f{i}" for i in range(1, 320)]),
                "_": TS,
            }
            url = "https://16.push2.eastmoney.com/api/qt/clist/get?" + urllib.parse.urlencode(params)
            r = sess.get(url, timeout=12)
            return r.json()

        all_rows = []
        j1 = _one_page(1)
        data1 = j1.get("data", {}) or {}
        rows1 = data1.get("diff") or []
        total = int(data1.get("total") or 0)
        all_rows.extend(rows1)
        pages = max(1, (total + 499) // 500)
        log.info("[issuer_rating] push2直接拉 p1/%d rows=%d total=%d t=%.1fs",
                 pages, len(rows1), total, _t.time() - t0)
        _t.sleep(0.3)
        for pn in range(2, pages + 1):
            j = _one_page(pn)
            rows = j.get("data", {}).get("diff") or []
            all_rows.extend(rows)
            _t.sleep(0.25)
        elapsed = _t.time() - t0

        # 遍历每一行，扫描所有 f 列值寻找 AAA/AA+/AA/A+/BBB 评级样字符串
        # （债项评级和主体评级都是同格式，这里我们取到的可能是债项，但如果东财未来开放主体字段则自动兼容）
        rating_like = re.compile(r"^(AAA|AA\+|AA-|AA|A\+|A-|A|BBB\+|BBB-|BBB|BB|B|CCC|CC|C|D)[+\-]?$", re.IGNORECASE)
        result = {}
        cols_seen_rating = {}  # 统计哪列出现过评级字符串
        for row in all_rows:
            c_raw = str(row.get("f12") or "")
            c6m = re.search(r"(\d{6})", c_raw)
            if not c6m:
                continue
            c6 = c6m.group(1)
            for k, v in row.items():
                if k in ("f1","f2","f3","f4","f5","f6","f7","f8","f9","f10","f11",
                         "f12","f13","f14","f152"):  # 已知非评级字段跳过
                    continue
                if not isinstance(v, str):
                    continue
                vs = v.strip()
                if rating_like.match(vs):
                    cols_seen_rating[k] = cols_seen_rating.get(k, 0) + 1
                    result[c6] = _clean_rating(vs)  # 最后一行覆盖
        log.info("[issuer_rating] push2 rows=%d cols=%d t=%.1fs  候选评级列Top5=%s  命中主体评级样=%d",
                 len(all_rows), len(all_rows[0]) if all_rows else 0, elapsed,
                 sorted(cols_seen_rating.items(), key=lambda x: -x[1])[:5], len(result))

        # 东财公开API目前只有债项评级单列RATING（在 RPT_BOND_CB_LIST 里有），
        # push2 行情接口也没有主体评级独立字段；所以 len(result) 通常为 0。
        # 写缓存：空值也写（避免下一小时内反复打接口），但只缓存 TTL/6，给未来接口升级留机会
        if len(result) >= 50:
            _cache_put_inner(cache_key_full, max_age, result)
        else:
            _cache_put_inner(cache_key_full, max(max_age // 6, 600), result)  # 短于成功TTl
        return result
    except Exception as e:
        import traceback as _tb
        log.warning("[issuer_rating] bond_cov_comparison FAIL: %s",
                    _tb.format_exc(limit=2).strip()[-600:])
        # 记录失败标记，短 TTL 内不重试（冷却）
        try:
            _cache_put_inner(cache_key_fail, fail_ttl, int(time.time()))
        except Exception:
            pass
        return {}


def _cb_merge_issuer_rating(df):
    """合并 issuer_rating 主体评级。

    合并原则（与其他 merge 函数保持一致）：
      * df 侧已有非空 issuer_rating → 不覆盖（保守策略）；
      * 否则按 6 位 bond_code 匹配填值。
    """
    df = df.copy()
    N = len(df)
    if N == 0:
        return df
    if "issuer_rating" not in df.columns:
        df["issuer_rating"] = pd.NA

    c6_all = (
        df["bond_code"].astype(str).str.extract(r"(\d{6})", expand=False).fillna("").tolist()
        if "bond_code" in df.columns else [""] * N
    )
    ir_map = _cb_fetch_issuer_rating()
    if not ir_map:
        return df

    def _empty(a):
        if a is None: return True
        if isinstance(a, float) and pd.isna(a): return True
        s = str(a).strip()
        return s in ("", "-", "--", "\u2014", "nan", "None", "<NA>")

    filled = 0
    try:
        arr = df["issuer_rating"].tolist()
        for i in range(N):
            if not _empty(arr[i]): continue
            c6 = c6_all[i]
            if not c6: continue
            v = ir_map.get(c6)
            if not v: continue
            arr[i] = v
            filled += 1
        df["issuer_rating"] = arr
    except Exception as e:
        logging.getLogger(__name__).warning("_cb_merge_issuer_rating apply fail: %s", e)
    logging.getLogger(__name__).info("[issuer_rating] 合并 %d 只（源 %d 只）", filled, len(ir_map))
    return df


# akshare 的 rating / 票面利率 / 强赎触发计数 等字段经常缺项。
# 这里定义 "从 qmt_detail_service 合并策略"：
#   对于 ak 侧为空、qmt 侧非空的标准字段，ak 填值；
#   对于 qmt 特有的诊断字段（_qmt 后缀），直接追加列便于看"数据从哪来"。
QMT_CORE_MERGE_COLS = [
    # 高价值补字段（ak 经常缺）
    "rating", "issuer_rating", "coupon_pct",
    "conv_price", "maturity_price",
    "conv_start_date", "conv_end_date", "maturity_date",
    "call_trigger_price", "put_trigger_price",
    "list_date", "stock_name",
]
QMT_DIAG_COLS_SUFFIXES = (
    "_qmt",
    "_yi_qmt",
    "_agency",
    "_agency_alt",
    "_alt",
)
QMT_META_ATTRS = {  # 把合并覆盖统计挂在 DataFrame.attrs 上
    "QMT_META": "__qmt_meta__",
    "QMT_FILLED": "__qmt_filled_counts__",
}


def _cached(key, ttl, fn, *args, **kwargs):
    """简易内存缓存。"""
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn(*args, **kwargs)
    with _lock:
        _cache[key] = (now, val)
    return val


def stock_spot(code: str) -> dict:
    """A股现货快照。code: '600000.SH' """
    key = code.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    df = _cached("a_spot", Config.AK_CACHE_TTL, ak.stock_zh_a_spot_em)
    row = df[df["代码"].astype(str) == key]
    if not len(row):
        return {}
    r = row.iloc[0].to_dict()
    r["code"] = code
    return r


def cb_panorama() -> pd.DataFrame:
    """可转债全景表（主表 + 现货量额合并）。"""
    df_main = _cached("cb_main", Config.AK_CACHE_TTL, ak.bond_cb_redeem_jsl)
    try:
        df_spot = _cached("cb_spot", Config.AK_CACHE_TTL, ak.bond_zh_hs_cov_spot)
    except Exception:
        df_spot = pd.DataFrame()

    def _suf(c):
        s = str(c).strip()
        if s.endswith((".SH", ".SZ", ".BJ")):
            return s.upper()
        return s + (".SH" if s[0] in "1569" else ".SZ")
    if "代码" in df_main.columns:
        df_main = df_main.copy()
        df_main["code"] = df_main["代码"].apply(_suf)
    if "code" in df_spot.columns:
        df_spot = df_spot.copy()
        df_spot["code"] = df_spot["code"].astype(str).apply(_suf)

    if len(df_spot):
        return df_main.merge(df_spot, on="code", how="left", suffixes=("", "_spot"))
    return df_main


def cb_top_dual_low(n: int = 10) -> pd.DataFrame:
    """双低排名 Top N。"""
    df = cb_panorama()
    pcol = next((c for c in ["价格", "现价", "最新价"] if c in df.columns), None)
    prem = next((c for c in ["溢价率", "溢价率%", "转股溢价率"] if c in df.columns), None)
    if not pcol or not prem:
        return df.head(0)
    df = df.copy()
    df["_p"] = pd.to_numeric(df[pcol], errors="coerce")
    df["_prem"] = pd.to_numeric(df[prem].astype(str).str.replace("%", "", regex=False), errors="coerce")
    df["双低值"] = df["_p"] + df["_prem"].fillna(100)
    return df.sort_values("双低值").head(n)




# ============================================================================
# PyTDX 实时行情 4 列（通达信原生协议，3秒轮询不封IP）
#   转债/正股涨跌幅 = (close/price - pre_close) / pre_close * 100
#   转债/正股振幅   = (high - low) / pre_close * 100
# ============================================================================
_PYTDX_SERVERS = [
    ("60.12.136.250", 7709),
    ("115.238.56.198", 7709),
    ("218.75.126.9",   7709),
    ("112.74.214.43",  7709),
    ("180.153.18.170", 7709),
    ("119.147.212.81", 7709),
    ("119.147.212.81", 7711),
    ("14.215.128.18",  7709),
    ("59.173.18.140",  7709),
    ("47.103.78.192",  7709),
]


# ============================================================================
# PyTDX 服务器延迟优选：启动期对每个服务器做 TCP 连通测速并升序排序。
# _pytdx_get_api() 优先从前 TOP_N 台最快服务器里随机选，避免冷启动抽到慢服务器
# （此前首次请求会卡 19s）。测速在后台守护线程进行，不阻塞首个请求。
# ============================================================================
import socket as _tdx_sock
_PYTDX_TOP_N = int(getattr(Config, "RT_SERVER_TOP_N", 4) or 4)
_PYTDX_SERVERS_RANKED = None
_PYTDX_RANK_LOCK = threading.Lock()

def _pytdx_measure_one(host, port, timeout=2.0):
    """测单台服务器 TCP 连通延迟（毫秒），不可达返回 None。"""
    t0 = time.time()
    try:
        s = _tdx_sock.create_connection((host, port), timeout=timeout)
        s.close()
        return (time.time() - t0) * 1000.0
    except Exception:
        return None

def _pytdx_rank_servers(force=False):
    """对 _PYTDX_SERVERS 做连通测速排序，返回 [(ms,(host,port)), ...] 升序。结果缓存。"""
    global _PYTDX_SERVERS_RANKED
    if _PYTDX_SERVERS_RANKED is not None and not force:
        return _PYTDX_SERVERS_RANKED
    with _PYTDX_RANK_LOCK:
        if _PYTDX_SERVERS_RANKED is not None and not force:
            return _PYTDX_SERVERS_RANKED
        measured = []
        for (h, p) in _PYTDX_SERVERS:
            measured.append((_pytdx_measure_one(h, p, timeout=2.0), (h, p)))
        measured.sort(key=lambda x: (x[0] if x[0] is not None else 1e9))
        _PYTDX_SERVERS_RANKED = measured
        logging.getLogger(__name__).info(
            "[pytdx] server rank(ms): %s",
            [(round(m, 1) if m is not None else None, s) for m, s in measured])
        return _PYTDX_SERVERS_RANKED

def _pytdx_start_rank_bg():
    """后台守护线程触发一次测速排序（不阻塞首个请求）。"""
    if not _PYTDX_OK:
        return
    try:
        _t = threading.Thread(target=_pytdx_rank_servers, kwargs={"force": True}, daemon=True)
        _t.start()
    except Exception:
        pass


# cb_full 并发编排线程池（max_workers=2：静态快照 / PyTDX 抓取 两个任务并行）
_cb_par_exec = _futures.ThreadPoolExecutor(max_workers=2)

def _safe_codes(df, col):
    if col in df.columns:
        return df[col].astype(str).tolist()
    return []

def _cb_static_codes_cached():
    """从内存缓存取出静态快照的代码 universe（忽略 TTL，取最近一次即可）。
    用于并发化：让 PyTDX 抓取提前于 akshare 静态重建启动。universe 变化极慢，
    毫秒级过期不影响覆盖。无缓存时返回 None（走串行兜底）。"""
    with _lock:
        h = _cache.get(CB_FULL_STATIC_KEY, None)
        if isinstance(h, (tuple, list)) and len(h) >= 2 and isinstance(h[1], pd.DataFrame) and len(h[1]) > 0:
            df = h[1]
            return (_safe_codes(df, "bond_code"), _safe_codes(df, "stock_code"))
    return None


# ============================================================================
# PyTDX 线程级连接池：复用持久连接，失效自动重连 + 轮换服务器
# 启动期触发服务器测速排序（后台守护线程，不阻塞首个请求；优先快服务器消除冷启动卡顿）
if _PYTDX_OK:
    _pytdx_start_rank_bg()
#   消除 _cb_fetch_rt_pytdx / get_kline_pytdx 每次调用都 new api / connect / disconnect 的握手开销
#   每个工作线程持有一个独立连接（threading.local），通过 _pytdx_get_api() 获取/复用
# ============================================================================
import random as _rnd_pool
_PYTDX_POOL = threading.local()


def _pytdx_is_alive(api):
    """判断 PyTDX 连接是否仍存活（socket 已连且未断开）"""
    if api is None:
        return False
    try:
        c = getattr(api, "client", None)
        return c is not None and getattr(c, "is_connected", lambda: False)()
    except Exception:
        return False


def _pytdx_get_api():
    """返回当前线程复用的 PyTDX 连接；失效则重连（随机轮换服务器）。返回 api 或 None。"""
    if not _PYTDX_OK:
        return None
    api = getattr(_PYTDX_POOL, "api", None)
    if _pytdx_is_alive(api):
        return api
    # 连接失效（或首次）：先断开旧连接再重连
    if api is not None:
        try:
            api.disconnect()
        except Exception:
            pass
        _PYTDX_POOL.api = None
    # 延迟优选：若后台测速已完成，优先前 TOP_N 台最快服务器（其间随机打乱分散负载）；
    # 若尚未测速完成（冷启动首个请求），直接用全部服务器随机连，避免同步测速阻塞 ~19s。
    ranked = _PYTDX_SERVERS_RANKED
    if ranked:
        top = [s for _, s in ranked[:_PYTDX_TOP_N]]
        servers = list(top)
        _rnd_pool.shuffle(servers)
        for host, port in servers:
            try:
                a = _TdxHq(raise_exception=False)
                if a.connect(host, port, time_out=3):
                    _PYTDX_POOL.api = a
                    _PYTDX_POOL.host = (host, port)
                    return a
            except Exception:
                continue
        rest = [s for _, s in ranked[_PYTDX_TOP_N:]]
        for host, port in rest:
            try:
                a = _TdxHq(raise_exception=False)
                if a.connect(host, port, time_out=3):
                    _PYTDX_POOL.api = a
                    _PYTDX_POOL.host = (host, port)
                    return a
            except Exception:
                continue
        return None
    # 冷启动兜底：未测速，直接用全部服务器随机连（不触发同步测速，保持首个请求快）
    servers = list(_PYTDX_SERVERS)
    _rnd_pool.shuffle(servers)
    for host, port in servers:
        try:
            a = _TdxHq(raise_exception=False)
            if a.connect(host, port, time_out=3):
                _PYTDX_POOL.api = a
                _PYTDX_POOL.host = (host, port)
                return a
        except Exception:
            continue
    return None


def _pytdx_market_and_code6(code_with_suffix):
    """'113697.SH' -> (market_id, '113697')
    market_id: 1=上证A股  0=深证A股  11=上证转债/债  12=深证转债/债
    """
    s = str(code_with_suffix).strip().upper()
    if not s: return None, ""
    parts = s.split(".")
    if len(parts) != 2: return None, parts[0][:6]
    code6, exch = parts[0][-6:], parts[1]
    try:
        head2 = code6[:2]
        head1 = code6[:1]
        if exch == "SH":
            # 上证转债/债：100xxx / 10xxxx / 11xxxx / 12xxxx / 13xxxx / 14xxxx / 15xxxx / 16xxxx / 17xxxx / 18xxxx / 19xxxx
            if head1 in ("1","2") or head2 in ("01","02","03","04","05","06","07","08","09","10","11","12","13","14","15","16","17","18","19",
                                                    "20","21","22","29"):
                # 上证转债常见：110xxx/111xxx/113xxx/118xxx/132xxx 等 → 市场11
                return 11, code6
            # A股：5xxxx(ETF)/6xxxx(A/科创/主板)/9xxxx(B股)
            return 1, code6
        else:  # SZ
            # 深证转债：12xxxx
            if head1 == "1" or head1 == "2":
                return 12, code6
            # 深A：0xxxx / 3xxxx
            return 0, code6
    except Exception:
        return None, code6


def _cb_fetch_rt_pytdx(bond_codes, stock_codes, threads=None, max_age=3):
    """并行 pytdx.get_security_bars(9, mkt, code, 0, 2) 拉 2 根日K
    → 实时 bars[0] = 当日（或最近一个交易日）K（close=最新价），bars[1].close=昨收
    返回 (bond_map, stock_map, meta)：
        bond_map  = {'113697.SH': {'bond_price':164.038, 'bond_chg_pct':0.33, 'amplitude_pct':2.6,
                                   'high':165.0, 'low':160.756, 'open':162.0, 'pre_close':163.5}}
        stock_map = {'600519.SH':  {'stock_price':..., 'stock_chg_pct':..., 'stock_amplitude':...,
                                    'high':..., 'low':..., 'open':..., 'pre_close':...}}
        meta = {'servers_used': ..., 'elapsed_ms':..., 'ok_bonds':N, 'ok_stocks':M, ...}
    """
    log = logging.getLogger(__name__)
    if not _PYTDX_OK:
        return {}, {}, {"ok": False, "reason": "pytdx 未安装或 import 失败"}
    # 缓存（内存，max_age秒）
    cache_key = "pytdx_rt_v1_" + "|".join(sorted(set(list(bond_codes)+list(stock_codes))))[:80]
    cached = _cache_get_inner(cache_key, max_age)
    if isinstance(cached, tuple) and len(cached) == 3:
        log.debug("[pytdx rt] cache hit: bonds=%d stocks=%d", len(cached[0]), len(cached[1]))
        return cached
    t0 = time.time()
    # 去重
    bond_list = list(dict.fromkeys([str(c) for c in bond_codes if str(c).strip()]))
    stock_list = list(dict.fromkeys([str(c) for c in stock_codes if str(c).strip()]))
    all_codes = [(c, "B") for c in bond_list] + [(c, "S") for c in stock_list]
    # worker：复用当前线程的持久连接（由 _pytdx_get_api 提供），避免反复握手
    #   连接中途失效时自动重连 + 整体重试一次
    def _worker_chunk(chunk_items):
        api = _pytdx_get_api()
        if api is None:
            return {"__err__": "no-pytdx-conn"}
        chunk_out = {}
        last_err = None
        for _round in range(2):
            try:
                if not _pytdx_is_alive(api):
                    api = _pytdx_get_api()
                    if api is None:
                        return {"__err__": "reconnect-fail-%s" % last_err}
                conn_died = False
                for code_full, side in chunk_items:
                    mkt, c6 = _pytdx_market_and_code6(code_full)
                    if mkt is None or not c6:
                        chunk_out[(code_full, side)] = None; continue
                    try:
                        bars = api.get_security_bars(9, mkt, c6, 0, 2)
                    except Exception as _be:
                        last_err = str(_be)
                        # 连接级异常：断开后整体重连重试一次
                        try: api.disconnect()
                        except: pass
                        _PYTDX_POOL.api = None
                        conn_died = True
                        break
                    if not bars or len(bars) < 1:
                        chunk_out[(code_full, side)] = None; continue
                    b0 = bars[0]
                    try:
                        opn = float(b0.get("open") or float("nan"))
                        hi  = float(b0.get("high") or float("nan"))
                        lo  = float(b0.get("low")  or float("nan"))
                        close = float(b0.get("close") or float("nan"))
                        # 昨收：优先 bars[1].close（次新债可能只有1根K）
                        if len(bars) >= 2:
                            lc = float(bars[1].get("close") or float("nan"))
                        else:
                            lc = float("nan")
                        chunk_out[(code_full, side)] = {
                            "open": opn, "high": hi, "low": lo, "close": close,
                            "pre_close": lc, "vol": b0.get("vol"), "amount": b0.get("amount"),
                            "date": b0.get("datetime") or b0.get("date"),
                        }
                    except Exception:
                        chunk_out[(code_full, side)] = None
                if not conn_died:
                    return chunk_out
                # 连接中途失效：重连后下一轮整体重试
                api = _pytdx_get_api()
                if api is None:
                    return {"__err__": "reconnect-fail-%s" % last_err}
            except Exception as _ce:
                last_err = "server-exc-%s" % _ce
                try: api.disconnect()
                except: pass
                _PYTDX_POOL.api = None
                api = _pytdx_get_api()
                if api is None:
                    return {"__err__": last_err}
        return {"__err__": last_err or "all-round-fail"}

    # 分 chunk：每 chunk 一批请求（越小并行度越高；受 Config.RT_CHUNK 约束）
    CHUNK = int(getattr(Config, "RT_CHUNK", 14) or 14)
    chunks = [all_codes[i:i+CHUNK] for i in range(0, len(all_codes), CHUNK)]
    # 线程池：并行度受 Config.RT_WORKERS 上限约束
    _rt_threads = int(threads if isinstance(threads, int) and threads > 0 else (getattr(Config, "RT_WORKERS", 48) or 48))
    results_all = {}
    with _futures.ThreadPoolExecutor(max_workers=max(1, min(_rt_threads, len(chunks)))) as ex:
        futs = [ex.submit(_worker_chunk, ch)
                for i, ch in enumerate(chunks)]
        for fu in _futures.as_completed(futs):
            try:
                res = fu.result(timeout=40)
                if isinstance(res, dict) and "__err__" not in res:
                    results_all.update(res)
                elif isinstance(res, dict):
                    log.debug("[pytdx rt] chunk fail: %s", res.get("__err__"))
            except Exception as _e:
                log.debug("[pytdx rt] worker chunk exc: %s", _e)
    # 拍平到 bond_map / stock_map，派生 4 列
    bond_map, stock_map = {}, {}
    ok_b = 0; ok_s = 0
    for (code_full, side), payload in results_all.items():
        if not isinstance(payload, dict):
            continue
        opn = payload.get("open"); hi = payload.get("high"); lo = payload.get("low")
        close = payload.get("close"); lc = payload.get("pre_close")
        # 派生
        chg = round((close - lc) / lc * 100, 3) if (lc and close and lc > 0 and not (lc != lc or close != close)) else float("nan")
        amp = round((hi - lo) / lc * 100, 3) if (lc and hi and lo and lc > 0 and not (lc != lc or hi != hi or lo != lo)) else float("nan")
        if side == "B":
            bond_map[code_full] = {
                "bond_price": close, "bond_chg_pct": chg, "amplitude_pct": amp,
                "high": hi, "low": lo, "open": opn, "pre_close_rt": lc,
                "vol": payload.get("vol"), "amount": payload.get("amount"),
            }
            if not (chg != chg): ok_b += 1  # not NaN
        else:
            stock_map[code_full] = {
                "stock_price": close, "stock_chg_pct": chg, "stock_amplitude": amp,
                "stock_high": hi, "stock_low": lo, "stock_open": opn, "stock_pre_close_rt": lc,
                "stock_vol": payload.get("vol"), "stock_amount": payload.get("amount"),
            }
            if not (chg != chg): ok_s += 1
    meta = {
        "ok": True,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "servers_cnt": len(_PYTDX_SERVERS),
        "threads": max(1, min(_rt_threads, len(chunks))),
        "requests_total": len(all_codes),
        "requests_ok": len(results_all),
        "bonds_chg_ok": ok_b, "stocks_chg_ok": ok_s,
        "distinct_bonds": len(bond_list), "distinct_stocks": len(stock_list),
    }
    log.info("[pytdx rt] t=%dms bonds=%d(%d ok) stocks=%d(%d ok) req=%d/%d threads=%d",
             meta["elapsed_ms"], len(bond_list), ok_b, len(stock_list), ok_s,
             meta["requests_ok"], meta["requests_total"], meta["threads"])
    # 写缓存
    _cache_put_inner(cache_key, max_age, (bond_map, stock_map, meta))
    return bond_map, stock_map, meta


def _pytdx_quote_market_and_code6(code_with_suffix):
    """get_security_quotes 用的市场映射（交易所级）：SH->1, SZ->0。
    与 _pytdx_market_and_code6（债券用 11/12）不同：quotes 接口对债券价格会按品种缩放、
    且 servertime 乱码，故本函数仅用于股票，债券一律走 get_security_bars。"""
    s = str(code_with_suffix).strip().upper()
    if not s:
        return None, ""
    parts = s.split(".")
    if len(parts) != 2:
        return None, parts[0][:6]
    code6, exch = parts[0][-6:], parts[1]
    if exch == "SH":
        return 1, code6
    if exch == "SZ":
        return 0, code6
    return None, code6


def _cb_rt_one_bars_stock(code_full):
    """单支股票用 get_security_bars 兜底（quotes 异常/缺失时），返回 stock_map 条目或 None。
    股票市场号走 1/0，价格干净（factor=1），与原 _cb_fetch_rt_pytdx 的股票分支一致。"""
    mkt, c6 = _pytdx_market_and_code6(code_full)
    if mkt is None or not c6:
        return None
    api = _pytdx_get_api()
    if api is None:
        return None
    try:
        bars = api.get_security_bars(9, mkt, c6, 0, 2)
    except Exception:
        return None
    if not bars or len(bars) < 1:
        return None
    b0 = bars[0]
    try:
        opn = float(b0.get("open") or float("nan"))
        hi = float(b0.get("high") or float("nan"))
        lo = float(b0.get("low") or float("nan"))
        close = float(b0.get("close") or float("nan"))
        lc = float(bars[1].get("close") or float("nan")) if len(bars) >= 2 else float("nan")
        chg = round((close - lc) / lc * 100, 3) if (lc and close and lc > 0 and not (lc != lc or close != close)) else float("nan")
        amp = round((hi - lo) / lc * 100, 3) if (lc and hi and lo and lc > 0 and not (lc != lc or hi != hi or lo != lo)) else float("nan")
        return {
            "stock_price": close, "stock_chg_pct": chg, "stock_amplitude": amp,
            "stock_high": hi, "stock_low": lo, "stock_open": opn, "stock_pre_close_rt": lc,
            "stock_vol": b0.get("vol"), "stock_amount": b0.get("amount"),
        }
    except Exception:
        return None


def _cb_fetch_rt_pytdx_quotes(stock_codes, threads=None, max_age=3):
    """批量 get_security_quotes 拉股票实时报价（单批 <=80 条，PyTDX 协议硬上限）。
    注意：get_security_quotes 对股票价格干净（factor=1、servertime 正常），对债券会缩放且时间戳损坏，
    故本函数仅用于股票；债券请走 get_security_bars（_cb_fetch_rt_pytdx）。
    任一批/任一条失败则回退该股票的 get_security_bars，保证零回归风险。
    返回 (stock_map, meta)；stock_map schema 与 _cb_fetch_rt_pytdx 的股票分支完全一致。
    """
    log = logging.getLogger(__name__)
    if not _PYTDX_OK:
        return {}, {"ok": False, "reason": "pytdx 未安装或 import 失败"}
    cache_key = "pytdx_q_v1_" + "|".join(sorted(set(str(c) for c in stock_codes)))[:80]
    cached = _cache_get_inner(cache_key, max_age)
    if isinstance(cached, tuple) and len(cached) == 2:
        log.debug("[pytdx quotes] cache hit: stocks=%d", len(cached[0]))
        return cached
    t0 = time.time()
    stock_list = list(dict.fromkeys([str(c) for c in stock_codes if str(c).strip()]))
    stock_map = {}
    req_total = 0
    req_ok = 0
    BATCH = 80  # PyTDX get_security_quotes 单批硬上限
    for i in range(0, len(stock_list), BATCH):
        batch = stock_list[i:i + BATCH]
        tuples = []
        for code_full in batch:
            mkt, c6 = _pytdx_quote_market_and_code6(code_full)
            if mkt is None or not c6:
                continue
            tuples.append((mkt, c6, code_full))
        if not tuples:
            continue
        req_total += 1
        api = _pytdx_get_api()
        if api is None:
            for _, _, cf in tuples:
                r = _cb_rt_one_bars_stock(cf)
                if isinstance(r, dict):
                    stock_map[cf] = r
                    req_ok += 1
            continue
        try:
            resp = api.get_security_quotes([(mkt, c6) for mkt, c6, _ in tuples])
        except Exception:
            for _, _, cf in tuples:
                r = _cb_rt_one_bars_stock(cf)
                if isinstance(r, dict):
                    stock_map[cf] = r
                    req_ok += 1
            continue
        if not resp:
            for _, _, cf in tuples:
                r = _cb_rt_one_bars_stock(cf)
                if isinstance(r, dict):
                    stock_map[cf] = r
                    req_ok += 1
            continue
        by_code = {}
        for r in resp:
            try:
                by_code[(int(r.get("market")), str(r.get("code")))] = r
            except Exception:
                pass
        for mkt, c6, code_full in tuples:
            r = by_code.get((mkt, c6))
            if r is None:
                rb = _cb_rt_one_bars_stock(code_full)
                if isinstance(rb, dict):
                    stock_map[code_full] = rb
                    req_ok += 1
                continue
            try:
                opn = float(r.get("open") or float("nan"))
                hi = float(r.get("high") or float("nan"))
                lo = float(r.get("low") or float("nan"))
                pr = r.get("price")
                close = float(pr if pr is not None else r.get("close") or float("nan"))
                lc = float(r.get("last_close") or float("nan"))
                vol = r.get("vol") or r.get("volume")
                amount = r.get("amount") or r.get("turnover")
                chg = round((close - lc) / lc * 100, 3) if (lc and close and lc > 0 and not (lc != lc or close != close)) else float("nan")
                amp = round((hi - lo) / lc * 100, 3) if (lc and hi and lo and lc > 0 and not (lc != lc or hi != hi or lo != lo)) else float("nan")
                stock_map[code_full] = {
                    "stock_price": close, "stock_chg_pct": chg, "stock_amplitude": amp,
                    "stock_high": hi, "stock_low": lo, "stock_open": opn, "stock_pre_close_rt": lc,
                    "stock_vol": vol, "stock_amount": amount,
                }
                if not (chg != chg):
                    req_ok += 1
            except Exception:
                rb = _cb_rt_one_bars_stock(code_full)
                if isinstance(rb, dict):
                    stock_map[code_full] = rb
                    req_ok += 1
    meta = {
        "ok": True,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "route": "get_security_quotes",
        "requests_total": req_total,
        "requests_ok": req_ok,
        "stocks_chg_ok": req_ok,
        "distinct_stocks": len(stock_list),
    }
    log.info("[pytdx quotes] t=%dms stocks=%d(%d ok) req=%d",
             meta["elapsed_ms"], len(stock_list), req_ok, req_total)
    _cache_put_inner(cache_key, max_age, (stock_map, meta))
    return stock_map, meta


def _cb_rt_combine(bars_rt, quotes_rt):
    """合并 债券bars路由 与 股票quotes路由 的结果。
    bars_rt   = (bond_map, stock_map_from_bars, meta_bars)
    quotes_rt = (stock_map_from_quotes, meta_quotes)
    股票以 quotes 为准（已验证干净），quotes 缺失/回退时由 bars 的股票结果兜底。
    返回统一的 (bond_map, stock_map, meta)，可直接喂给 _cb_merge_rt_pytdx。
    """
    if not isinstance(bars_rt, (tuple, list)) or len(bars_rt) < 3:
        bars_rt = ({}, {}, {})
    b_bond, b_stock, b_meta = (list(bars_rt) + [{}, {}, {}])[:3]
    q_stock = quotes_rt[0] if isinstance(quotes_rt, (tuple, list)) and len(quotes_rt) >= 1 else {}
    q_meta = quotes_rt[1] if isinstance(quotes_rt, (tuple, list)) and len(quotes_rt) >= 2 else {}
    b_meta = b_meta or {}
    q_meta = q_meta or {}
    # 股票：quotes 优先覆盖，bars 兜底
    stock_map = dict(b_stock or {})
    stock_map.update(q_stock or {})
    meta = {
        "ok": bool(b_meta.get("ok")) or bool(q_meta.get("ok")),
        "route": "split(bars_bonds+quotes_stocks)",
        "elapsed_ms": max(int(b_meta.get("elapsed_ms") or 0), int(q_meta.get("elapsed_ms") or 0)),
        "requests_total": int(b_meta.get("requests_total") or 0) + int(q_meta.get("requests_total") or 0),
        "requests_ok": int(b_meta.get("requests_ok") or 0) + int(q_meta.get("requests_total") or 0),
        "bonds_chg_ok": int(b_meta.get("bonds_chg_ok") or 0),
        "stocks_chg_ok": int(b_meta.get("stocks_chg_ok") or 0) + int(q_meta.get("stocks_chg_ok") or 0),
        "distinct_bonds": int(b_meta.get("distinct_bonds") or 0),
        "distinct_stocks": int(b_meta.get("distinct_stocks") or 0) + int(q_meta.get("distinct_stocks") or 0),
        "bond_route": b_meta.get("route") or "get_security_bars",
        "stock_route": q_meta.get("route") or "get_security_quotes",
    }
    return b_bond or {}, stock_map, meta


def _cb_fetch_rt_pytdx_split(bond_codes, stock_codes, threads=None, max_age=3):
    """债券走 get_security_bars（价格已验证正确）、股票走批量 get_security_quotes（factor=1，干净）两路并行，
    再合并为统一 (bond_map, stock_map, meta)。对外接口与 _cb_fetch_rt_pytdx 一致，
    由 cb_full_metrics 调用以在保持正确性的前提下把股票请求从逐支 bars 降为 80/批 quotes。"""
    if not _PYTDX_OK:
        return {}, {}, {"ok": False, "reason": "pytdx 未安装或 import 失败"}
    bonds = list(dict.fromkeys([str(c) for c in (bond_codes or []) if str(c).strip()]))
    stocks = list(dict.fromkeys([str(c) for c in (stock_codes or []) if str(c).strip()]))
    with _futures.ThreadPoolExecutor(max_workers=2) as ex:
        fb = ex.submit(_cb_fetch_rt_pytdx, bonds, []) if bonds else None
        fq = ex.submit(_cb_fetch_rt_pytdx_quotes, stocks) if stocks else None
        bars_rt = fb.result() if fb is not None else ({}, {}, {"ok": True})
        quotes_rt = fq.result() if fq is not None else ({}, {"ok": True})
    return _cb_rt_combine(bars_rt, quotes_rt)



_KL_CACHE = {}


def get_kline_pytdx(code, count=120, ttl=120, batch=800, max_bars=5000):
    """PyTDX 日K（最可靠路径：与 /cb/full 实时同源，无需登录 miniQMT）。

    count 较大时（超过 PyTDX 单次 get_security_bars 上限约 800 根）自动循环
    start 偏移分批拉取，从而支持「全量」历史 K 线。
    """
    if not _PYTDX_OK:
        return []
    mkt, c6 = _pytdx_market_and_code6(code)
    if mkt is None or not c6:
        return []
    cnt = min(int(count), max_bars)
    key = "kline_%s_%d" % (code, cnt)
    cached = _KL_CACHE.get(key)
    if cached and (time.time() - cached[1]) < ttl:
        return cached[0]
    out = []
    api = _pytdx_get_api()
    for _round in range(2):
        if api is None:
            break
        try:
            if not _pytdx_is_alive(api):
                api = _pytdx_get_api()
                if api is None:
                    break
            # PyTDX 单次 get_security_bars 上限约 800 根；count 较大时循环 start 偏移分批拉全量
            allbars = []
            need = cnt
            start = 0
            while need > 0:
                take = min(batch, need)
                bars = api.get_security_bars(9, mkt, c6, start, take)
                if not bars:
                    break
                allbars.extend(bars)
                got = len(bars)
                start += got
                need -= got
                if got < take:
                    break
            if not allbars:
                # 空结果可能因连接失效：重连后重试一次
                try: api.disconnect()
                except: pass
                _PYTDX_POOL.api = None
                api = _pytdx_get_api()
                continue
            for b in allbars:
                try:
                    dt = str(b.get("datetime") or b.get("date") or "")[:10]
                    out.append({
                        "date": dt,
                        "open": float(b.get("open")),
                        "high": float(b.get("high")),
                        "low": float(b.get("low")),
                        "close": float(b.get("close")),
                        "vol": float(b.get("vol") or 0),
                        "amount": float(b.get("amount") or 0),
                    })
                except Exception:
                    continue
            break
        except Exception:
            try: api.disconnect()
            except Exception: pass
            _PYTDX_POOL.api = None
            api = _pytdx_get_api()
    if out:
        out.sort(key=lambda x: x["date"])
        _KL_CACHE[key] = (out, time.time())
    return out


# ============================================================================
# 实时行情 优先路径 A：QMT HTTP 桥 v3 /cb_rt_snapshot（iQuant 内部批量拉，<500ms）
#     失败 / 覆盖率不足 → fallback PyTDX（路径 B）
# ============================================================================
_CB_RT_BRIDGE_CACHE = {}       # 短缓存：__rt_bridge_data / __rt_bridge_ts
_CB_RT_BRIDGE_CACHE_TTL = 2.5  # 秒；比 PyTDX 内部 3s 略短，保证取到最接近真实的


def _cb_fetch_rt_qmt_bridge(df, bridge_url=None, token=None, timeout=None):
    """调用 58080 桥 /cb_rt_snapshot。
    返回 (bond_map, stock_map, meta_dict_or_None)；失败返回 ({}, {}, None)
    """
    bridge_url = bridge_url or getattr(Config, "BRIDGE_URL", "http://127.0.0.1:58080")
    token = token or getattr(Config, "BRIDGE_TOKEN", "change-me-to-your-token")
    timeout = timeout or getattr(Config, "CB_RT_BRIDGE_TIMEOUT", 8)

    # ---- 短内存缓存（前端 setInterval 3s 时命中 0ms）----
    import time as _t
    now = _t.time()
    cached = _CB_RT_BRIDGE_CACHE.get("data")
    ts = _CB_RT_BRIDGE_CACHE.get("ts") or 0
    if cached and now - ts < _CB_RT_BRIDGE_CACHE_TTL:
        return cached.get("bond_map", {}), cached.get("stock_map", {}), cached.get("meta", {})

    import logging as _lg
    log = _lg.getLogger(__name__)

    # ---- 从 df 取出需要的代码列表 ----
    try:
        import pandas as pd
        if not isinstance(df, pd.DataFrame) or len(df) == 0:
            return {}, {}, None
        bond_codes = [str(c) for c in df["bond_code"].astype(str).unique().tolist() if c and str(c) != "nan"]
        stock_codes = []
        if "stock_code" in df.columns:
            stock_codes = [str(c) for c in df["stock_code"].dropna().astype(str).unique().tolist()
                         if c and str(c) != "nan"]
    except Exception as e:
        log.warning("[bridge rt] df/col err: %s", e)
        return {}, {}, None
    if not bond_codes:
        return {}, {}, None

    # ---- 并发小批量打桥（全量 600+ 转债单次易超时，拆批后每批可控）----
    try:
        import requests as _rq
    except Exception:
        _rq = None
    if _rq is None:
        return {}, {}, None

    try:
        _BATCH = 120
        sess = _rq.Session()
        url = bridge_url.rstrip("/") + "/cb_rt_snapshot"
        headers = {"X-API-Token": token, "Content-Type": "application/json"}

        def _post(bc, sc):
            payload = {"bond_codes": bc, "stock_codes": sc}
            try:
                r = sess.post(url, json=payload, headers=headers, timeout=timeout)
                if r.status_code != 200:
                    log.warning("[bridge rt] HTTP %s: %s", r.status_code, r.text[:200])
                    return {}, {}
                js = r.json()
                if not js.get("ok"):
                    log.warning("[bridge rt] resp !ok: %s", str(js)[:200])
                    return {}, {}
                return js.get("bond_map") or {}, js.get("stock_map") or {}
            except Exception as e:
                log.warning("[bridge rt] batch fail: %s", e)
                return {}, {}

        # 转债批与正股批一一配对：每批 120 转债 + 120 正股，正股只查一次，避免冗余
        bond_chunks = [bond_codes[i:i + _BATCH] for i in range(0, len(bond_codes), _BATCH)] or [[]]
        stock_chunks = [stock_codes[i:i + _BATCH] for i in range(0, len(stock_codes), _BATCH)] or [[]]
        n = max(len(bond_chunks), len(stock_chunks))
        jobs = []
        for i in range(n):
            bc = bond_chunks[i] if i < len(bond_chunks) else []
            sc = stock_chunks[i] if i < len(stock_chunks) else []
            if not bc and not sc:
                continue
            jobs.append((bc, sc))
        t0 = _t.time()
        bond_map, stock_map = {}, {}
        with _futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(jobs)))) as ex:
            futs = [ex.submit(_post, bc, sc) for bc, sc in jobs]
            for fut in _futures.as_completed(futs):
                bm, sm = fut.result() or ({}, {})
                bond_map.update(bm)
                stock_map.update(sm)
        elapsed = int((_t.time() - t0) * 1000)
        if not bond_map:
            return {}, {}, None
        meta = {
            "_http_elapsed_ms": elapsed,
            "_src": "qmt_bridge_v3",
            "bond_cnt_req": len(bond_codes),
            "bond_cnt_ret": len(bond_map),
            "stock_cnt_req": len(stock_codes),
            "stock_cnt_ret": len(stock_map),
        }
        _CB_RT_BRIDGE_CACHE["data"] = {"bond_map": bond_map, "stock_map": stock_map, "meta": meta}
        _CB_RT_BRIDGE_CACHE["ts"] = now
        log.info("[bridge rt v3] %s bonds/%s stocks in %sms (coverage bond %s%% stock %s%%)",
                 len(bond_map), len(stock_map), elapsed,
                 int(len(bond_map) * 100 / max(1, len(bond_codes))),
                 int(len(stock_map) * 100 / max(1, len(stock_codes))))
        return bond_map, stock_map, meta
    except Exception as e:
        log.warning("[bridge rt] fail: %s (bridge=%s)", e, bridge_url)
        return {}, {}, None


def _cb_rt_bridge_coverage_ok(meta, bond_map, stock_map, df):
    """桥的覆盖率够不够直接用？阈值：转债 ≥85% 且 正股 ≥70%（正股有取不到/退市情况，稍放宽）。"""
    if not meta or not isinstance(bond_map, dict):
        return False
    try:
        bond_req = int(meta.get("bond_cnt_req") or 0)
        stock_req = int(meta.get("stock_cnt_req") or 0)
    except Exception:
        bond_req = stock_req = 0
    if bond_req == 0:
        return False
    bond_cov = len(bond_map) / bond_req
    stock_cov = (len(stock_map) / stock_req) if stock_req > 0 else 1.0
    return bond_cov >= 0.85 and stock_cov >= 0.70



def _cb_merge_rt_qmt_bridge(df, overwrite_if_filled=True):
    """和 _cb_merge_rt_pytdx 完全同语义（都往 df 写 bond_price/stock_price/... 覆盖列），
    数据源走 /cb_rt_snapshot 返回的 bond_map/stock_map。返回 df（不修改实参内存时copy）。"""
    if df is None or len(df) == 0:
        return df
    bond_map, stock_map, meta = _cb_fetch_rt_qmt_bridge(df)
    if not bond_map:
        df.attrs["__rt_src_meta__"] = {"_src": "qmt_bridge_v3_failed"}
        return df

    import pandas as pd
    import numpy as np

    def _ensure_float_col(df, col):
        if col not in df.columns:
            df[col] = np.nan
        try:
            if not pd.api.types.is_float_dtype(df[col]):
                df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
        except Exception:
            df[col] = np.nan
        return df

    def _set_if_blank(df, idx, col, val):
        if val is None:
            return False
        try:
            fv = float(val)
        except Exception:
            return False
        try:
            if not overwrite_if_filled:
                cur = df.at[idx, col]
                if isinstance(cur, float) and not pd.isna(cur):
                    return False
                if isinstance(cur, (int,)) and not pd.isna(cur):
                    return False
            df.at[idx, col] = fv
            return True
        except Exception:
            return False

    N = len(df)
    # ============== 转债合并 ==============
    for col in ["bond_chg_pct","amplitude_pct","bond_price","high","low","open",
                "daily_amplitude_calc","volume_zhang","amount_yuan"]:
        df = _ensure_float_col(df, col)
    fills_b = set()
    bc_arr = df["bond_code"].astype(str).values
    for i in range(N):
        idx = df.index[i]
        p = bond_map.get(bc_arr[i])
        if not isinstance(p, dict): continue
        if _set_if_blank(df, idx, "bond_chg_pct",    p.get("bond_chg_pct"),    ): fills_b.add("bond_chg_pct")
        if _set_if_blank(df, idx, "amplitude_pct",   p.get("amplitude_pct"),   ): fills_b.add("amplitude_pct")
        if _set_if_blank(df, idx, "bond_price",      p.get("bond_price"),      ): fills_b.add("bond_price")
        if _set_if_blank(df, idx, "high",            p.get("high"),            ): fills_b.add("high")
        if _set_if_blank(df, idx, "low",             p.get("low"),             ): fills_b.add("low")
        if _set_if_blank(df, idx, "open",            p.get("open"),            ): fills_b.add("open")
        if _set_if_blank(df, idx, "volume_zhang",    p.get("vol"),             ): fills_b.add("volume_zhang")
        if _set_if_blank(df, idx, "amount_yuan",     p.get("amount"),          ): fills_b.add("amount_yuan")
    import logging as _lgg
    _lgg.getLogger(__name__).info("[bridge rt 转债] 合并列: %s", sorted(fills_b))

    # ============== 正股合并 ==============
    if "stock_code" in df.columns:
        for col in ["stock_chg_pct","stock_amplitude","stock_price","stock_high","stock_low","stock_open",
                    "stock_volume_zhang","stock_amount_yuan"]:
            df = _ensure_float_col(df, col)
        fills_s = set()
        sc_arr = df["stock_code"].astype(str).values
        for i in range(N):
            idx = df.index[i]
            p = stock_map.get(sc_arr[i])
            if not isinstance(p, dict): continue
            if _set_if_blank(df, idx, "stock_chg_pct",        p.get("stock_chg_pct"),   ): fills_s.add("stock_chg_pct")
            if _set_if_blank(df, idx, "stock_amplitude",     p.get("stock_amplitude"), ): fills_s.add("stock_amplitude")
            if _set_if_blank(df, idx, "stock_price",         p.get("stock_price"),      ): fills_s.add("stock_price")
            if _set_if_blank(df, idx, "stock_high",          p.get("stock_high"),      ): fills_s.add("stock_high")
            if _set_if_blank(df, idx, "stock_low",           p.get("stock_low"),       ): fills_s.add("stock_low")
            if _set_if_blank(df, idx, "stock_open",          p.get("stock_open"),      ): fills_s.add("stock_open")
            if _set_if_blank(df, idx, "stock_volume_zhang",  p.get("stock_vol"),       ): fills_s.add("stock_volume_zhang")
            if _set_if_blank(df, idx, "stock_amount_yuan",   p.get("stock_amount"),    ): fills_s.add("stock_amount_yuan")
        _lgg.getLogger(__name__).info("[bridge rt 正股] 合并列: %s", sorted(fills_s))

    if meta:
        df.attrs["__rt_src_meta__"] = meta
    return df


def _cb_merge_rt_pytdx(df: pd.DataFrame, rt=None) -> pd.DataFrame:
    """把 PyTDX 返回的 bond_map / stock_map 合并到 DataFrame（安全 dtype 版）。

    填充列：
      - 转债: bond_chg_pct / amplitude_pct（优先填，空缺补）
              bond_price / high / low / open（仅 ak 空时补）
              daily_amplitude_calc 与 amplitude_pct 对齐
      - 正股: stock_chg_pct / stock_amplitude / stock_price
              stock_high / stock_low / stock_open
    """
    if df is None or len(df) == 0:
        return df
    if not _PYTDX_OK:
        df.attrs["__pytdx_meta__"] = {"ok": False, "reason": "pytdx not importable"}
        return df
    log = logging.getLogger(__name__)
    N = len(df)
    bc_list = df["bond_code"].astype(str).tolist() if "bond_code" in df.columns else []
    sc_list = df["stock_code"].astype(str).tolist() if "stock_code" in df.columns else []
    if rt is not None and len(rt) == 3:
        bond_map, stock_map, meta = rt
    else:
        bond_map, stock_map, meta = _cb_fetch_rt_pytdx(bc_list, sc_list)
    df.attrs["__pytdx_meta__"] = meta
    if not bond_map and not stock_map:
        return df

    def _is_blank(v):
        if v is None: return True
        if isinstance(v, float) and pd.isna(v): return True
        s = str(v).strip()
        return s in ("", "-", "--", "\u2014", "nan", "None", "<NA>", "0", "0.0", "0.00")

    def _ensure_float_col(df, col):
        """确保 df[col] 列 dtype 可容纳浮点数值（pandas3.x 禁止把 float 塞进 str dtype array）"""
        if col not in df.columns:
            df[col] = pd.Series([float("nan")] * N, dtype="float64", index=df.index)
            return df
        # 如果列 dtype 是 object/str：先转 numeric（不破坏已有合法数字）
        if df[col].dtype.kind in ("O", "S", "U"):
            # 先按位 "原值保留 if numeric else NaN"，再合并
            numeric_vals = pd.to_numeric(df[col], errors="coerce").astype("float64").values
            df[col] = numeric_vals
        elif df[col].dtype.kind in ("b",):
            df[col] = df[col].astype("float64")
        else:
            # int / uint 转 float（允许 NaN）
            if df[col].dtype.kind in ("i", "u", "f"):
                df[col] = df[col].astype("float64")
            else:
                try:
                    df[col] = df[col].astype("float64")
                except Exception:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def _set_if_blank(df, idx, col, nv, overwrite_if_numeric_filled=False):
        """单个 cell 赋值：安全按行/列名定位，确保列是 float dtype"""
        if nv is None: return False
        if isinstance(nv, float) and pd.isna(nv): return False
        if _is_blank(nv): return False
        try:
            fv = float(nv)
        except Exception:
            return False
        if fv != fv: return False  # NaN 过滤
        # 若 df[col] 当前类型不是 float，先转
        df = _ensure_float_col(df, col)  # noqa: F841  (df 本身就地 mutate，保持引用一致也行，这里显式确保)
        cur = df.at[idx, col]
        need_set = False
        if overwrite_if_numeric_filled:
            need_set = True  # 对于 chg_pct / amp_pct，直接填 pytdx（用户需求：这两列用 iQuant/pytdx）
        else:
            # price/high/low/open: ak 侧有值就不覆盖（保守）
            if _is_blank(cur): need_set = True
        if need_set:
            df.at[idx, col] = fv
            return True
        return False

    # ============== 转债合并 ==============
    if "bond_code" in df.columns:
        # 1) 预先确保所有目标列 dtype float（含成交量/成交额）
        for col in ["bond_chg_pct","amplitude_pct","bond_price","high","low","open",
                    "daily_amplitude_calc","volume_zhang","amount_yuan"]:
            df = _ensure_float_col(df, col)
        # 2) 逐行赋值
        fills_b = set()
        bc_arr = df["bond_code"].astype(str).values
        for i in range(N):
            idx = df.index[i]
            p = bond_map.get(bc_arr[i])
            if not isinstance(p, dict): continue
            # 全部实时列：直接用 PyTDX 覆盖（用户要求价格/量/额也用 PyTDX/iQuant）
            if _set_if_blank(df, idx, "bond_chg_pct",    p.get("bond_chg_pct"),    overwrite_if_numeric_filled=True): fills_b.add("bond_chg_pct")
            if _set_if_blank(df, idx, "amplitude_pct",   p.get("amplitude_pct"),   overwrite_if_numeric_filled=True): fills_b.add("amplitude_pct")
            if _set_if_blank(df, idx, "bond_price",      p.get("bond_price"),      overwrite_if_numeric_filled=True): fills_b.add("bond_price")
            if _set_if_blank(df, idx, "high",            p.get("high"),            overwrite_if_numeric_filled=True): fills_b.add("high")
            if _set_if_blank(df, idx, "low",             p.get("low"),             overwrite_if_numeric_filled=True): fills_b.add("low")
            if _set_if_blank(df, idx, "open",            p.get("open"),            overwrite_if_numeric_filled=True): fills_b.add("open")
            if _set_if_blank(df, idx, "volume_zhang",    p.get("vol"),             overwrite_if_numeric_filled=True): fills_b.add("volume_zhang")
            if _set_if_blank(df, idx, "amount_yuan",     p.get("amount"),           overwrite_if_numeric_filled=True): fills_b.add("amount_yuan")
        # 3) 同步 daily_amplitude_calc
        if "daily_amplitude_calc" in df.columns and "amplitude_pct" in df.columns:
            amp_vals = df["amplitude_pct"].values
            dac_arr = df["daily_amplitude_calc"].values.copy()
            for i in range(N):
                try:
                    cur = dac_arr[i]
                    nv  = amp_vals[i]
                    if _is_blank(cur) and not _is_blank(nv):
                        dac_arr[i] = float(nv)
                except Exception:
                    pass
            df["daily_amplitude_calc"] = dac_arr  # 已经 float dtype，直接赋值 OK
            fills_b.add("daily_amplitude_calc")
        log.info("[pytdx rt 转债] 合并列: %s", sorted(fills_b))

    # ============== 正股合并 ==============
    if "stock_code" in df.columns:
        for col in ["stock_chg_pct","stock_amplitude","stock_price","stock_high","stock_low","stock_open",
                    "stock_volume_zhang","stock_amount_yuan"]:
            df = _ensure_float_col(df, col)
        fills_s = set()
        sc_arr = df["stock_code"].astype(str).values
        for i in range(N):
            idx = df.index[i]
            p = stock_map.get(sc_arr[i])
            if not isinstance(p, dict): continue
            if _set_if_blank(df, idx, "stock_chg_pct",        p.get("stock_chg_pct"),   overwrite_if_numeric_filled=True): fills_s.add("stock_chg_pct")
            if _set_if_blank(df, idx, "stock_amplitude",     p.get("stock_amplitude"), overwrite_if_numeric_filled=True): fills_s.add("stock_amplitude")
            if _set_if_blank(df, idx, "stock_price",         p.get("stock_price"),      overwrite_if_numeric_filled=True): fills_s.add("stock_price")
            if _set_if_blank(df, idx, "stock_high",          p.get("stock_high"),      overwrite_if_numeric_filled=True): fills_s.add("stock_high")
            if _set_if_blank(df, idx, "stock_low",           p.get("stock_low"),       overwrite_if_numeric_filled=True): fills_s.add("stock_low")
            if _set_if_blank(df, idx, "stock_open",          p.get("stock_open"),      overwrite_if_numeric_filled=True): fills_s.add("stock_open")
            if _set_if_blank(df, idx, "stock_volume_zhang",  p.get("stock_vol"),       overwrite_if_numeric_filled=True): fills_s.add("stock_volume_zhang")
            if _set_if_blank(df, idx, "stock_amount_yuan",   p.get("stock_amount"),    overwrite_if_numeric_filled=True): fills_s.add("stock_amount_yuan")
        log.info("[pytdx rt 正股] 合并列: %s", sorted(fills_s))
    return df

def _cb_merge_rt_qmt_fallback(df: pd.DataFrame, rt_qmt) -> pd.DataFrame:
    """QMT 侧兜底（HTTP桥或58600提供rt时）合并，接口留待未来扩展。"""
    if not rt_qmt or not isinstance(rt_qmt, dict):
        return df
    meta = rt_qmt.pop("__meta__", {}) if "__meta__" in rt_qmt else {}
    df.attrs["__qmt_rt_meta__"] = meta
    return df


# ==========================================================================
# 可转债全指标
# ==========================================================================
def _norm_code(c):
    if pd.isna(c) or c is None:
        return ""
    s = str(c).strip()
    if s.endswith((".SH", ".SZ", ".BJ")):
        return s.upper()
    head = s[:1]
    if not head.isdigit():
        return s.upper()
    if s.startswith(("6","5","9","11","13","16","17")):
        return s + ".SH"
    return s + ".SZ"


def _num(x):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return float("nan")
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    if s in ("", "-", "--", "—", "None", "nan", "NaN"):
        return float("nan")
    s = s.replace("%", "").replace("亿", "").replace("张", "")
    try: return float(s)
    except Exception: return float("nan")


_CN_STD_MAP = [
    (("代码", "转债代码", "债券代码", "code"), "bond_code"),
    (("转债名称", "债券名称", "转债名", "名称", "name"), "bond_name"),
    (("正股代码", "stock_code"), "stock_code"),
    (("正股名称", "正股简称", "正股名", "stock_name"), "stock_name"),
    (("正股价", "正股最新价", "stock_price"), "stock_price"),
    (("价格", "现价", "最新价", "bond_price"), "bond_price"),
    (("前收盘", "昨收", "昨结", "settlement", "preclose"), "pre_close"),
    (("涨跌额", "change"), "change"),
    (("涨跌幅", "涨跌幅%", "chg", "bond_chg_pct"), "bond_chg_pct"),
    (("转股价", "最新转股价", "转换价格", "conv_price"), "conv_price"),
    (("转股比例", "转换比例", "convert_ratio"), "convert_ratio"),
    (("转股起始日", "转股开始日", "conv_start"), "conv_start_date"),
    (("转股截止日", "转股结束日", "conv_end"), "conv_end_date"),
    (("溢价率", "溢价率%", "转股溢价率", "conv_premium_pct"), "conv_premium_pct"),
    (("双低价", "双低值", "dual_low"), "dual_low"),
    (("剩余规模", "剩余规模(亿元)", "规模", "余额", "rem_size"), "rem_size_yi"),
    (("发行规模", "发行规模(亿元)", "issue_size"), "issue_size_yi"),
    (("发行价", "issue_price"), "issue_price"),
    (("上市日期", "上市日", "list_date"), "list_date"),
    (("到期时间",), "maturity_date"),
    (("债券简称",), "bond_name_ths"),
    (("正股简称",), "stock_name_ths"),
    (("实际发行量",), "issue_size_yi_ths"),
    (("到期日期", "到期日", "到期时间", "maturity", "expire"), "maturity_date"),
    (("发行年限", "duration", "term"), "term_year"),
    (("信用评级", "评级", "债券评级", "rating"), "rating"),
    (("发行主体信用评级", "主体评级", "主体评级_jsl", "issuer_rating"), "issuer_rating"),
    (("票面利率", "coupon", "coupon_rate", "票面利率(%)"), "coupon_pct"),
    (("到期赎回价", "到期赎回价格", "赎回价", "到期赎回价(元)", "redemption_price"), "maturity_price"),
    (("到期税前收益", "ytm_before_tax", "税前YTM", "到期收益率", "到期收益率(%)"), "ytm_before_tax_pct"),
    (("纯债价值", "pure_bond_value"), "pure_bond_value"),
    (("纯债溢价率", "pb_premium_pct"), "pure_bond_premium_pct"),
    (("到期税前收益", "ytm_before_tax", "税前YTM"), "ytm_before_tax_pct"),
    (("到期税后收益", "ytm_after_tax", "税后YTM"), "ytm_after_tax_pct"),
    (("强赎状态", "强赎", "callable_status"), "call_status"),
    (("强赎触发价", "强赎价", "call_price"), "call_trigger_price"),
    (("强赎天计数", "强赎天数", "已满足强赎天数", "已满足赎回天数", "强赎触发计数", "强赎天满足计数"), "call_trigger_days"),
    (("强赎最后交易日", "最后交易日", "赎回最后交易"), "call_last_trade_date"),
    (("回售触发价", "回售价格", "回售触发价_jsl", "put_price"), "put_trigger_price"),
    (("回售起始日", "回售开始", "put_start"), "put_start_date"),
    (("下修状态", "下修", "conv_price_adjust"), "adjust_status"),
    (("转股价下修触发价", "下修触发价"), "adjust_trigger_price"),
    (("成交额", "amount"), "amount_yuan"),
    (("成交量", "volume"), "volume_zhang"),
    (("最高", "最高价", "high"), "high"),
    (("最低", "最低价", "low"), "low"),
    (("今开", "开盘价", "open"), "open"),
    (("买一价", "bid1"), "bid1"),
    (("卖一价", "ask1"), "ask1"),
    (("换手率", "turnover_rate"), "turnover_pct"),
    (("振幅", "amplitude"), "amplitude_pct"),
]


def _apply_std_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    existed = set(df.columns)
    STR_COLS = {"bond_code", "stock_code", "bond_name", "stock_name", "rating",
                "issuer_rating", "call_status", "adjust_status"}
    for aliases, stdname in _CN_STD_MAP:
        if stdname in existed:
            continue
        hits = [a for a in aliases if a in existed]
        if not hits:
            continue
        if stdname.endswith("_date"):
            # coalesce: 逐列取第一个非空
            def _pick_date(row):
                for h in hits:
                    v = row[h]
                    if not pd.isna(v) and str(v).strip() not in ("", "-", "--"):
                        return str(v).strip()[:10]
                return ""
            df[stdname] = df.apply(_pick_date, axis=1)
        elif stdname in STR_COLS:
            def _pick_str(row):
                for h in hits:
                    v = row[h]
                    if not pd.isna(v) and str(v).strip() not in ("", "-", "--"):
                        return str(v).strip()
                return ""
            df[stdname] = df.apply(_pick_str, axis=1)
        else:
            # 数值列也 coalesce
            def _pick_num(row):
                for h in hits:
                    v = _num(row[h])
                    if not pd.isna(v):
                        return v
                return float("nan")
            df[stdname] = df.apply(_pick_num, axis=1)
    return df


def _add_derived(df: pd.DataFrame) -> pd.DataFrame:
    import math
    df = df.copy()
    extras = ["conv_value_100","conv_value_ratio_pct","conv_rate_per_100",
              "conv_premium_pct_calc","dual_low_calc","dual_low_rank",
              "ytm_before_tax_calc","redemption_gain_pct","coupon_sum_estimate",
              "maturity_days_left","maturity_years_left",
              "call_trigger_pct","call_progress_pct","put_trigger_pct","adjust_trigger_pct",
              "daily_amplitude_calc","turnover_rate_calc",
              "premium_decomp_conv_pct","premium_decomp_bond_pct",
              "remaining_ratio"]
    for c in extras:
        if c not in df.columns:
            df[c] = float("nan")

    today = dt.date.today()
    CALL_TRIGGER_NORM_DAYS = 15

    for idx in df.index:
        r = df.loc[idx]
        bc = str(r.get("bond_code") or r.get("代码") or "")
        if bc and not bc.endswith((".SH", ".SZ")):
            df.at[idx, "bond_code"] = _norm_code(bc)
        sc = str(r.get("stock_code") or "")
        if sc and not sc.endswith((".SH", ".SZ")):
            df.at[idx, "stock_code"] = _norm_code(sc)

        bp   = _num(r.get("bond_price") or r.get("价格"))
        sp   = _num(r.get("stock_price") or r.get("正股价"))
        cp   = _num(r.get("conv_price") or r.get("转股价"))
        rem  = _num(r.get("rem_size_yi") or r.get("剩余规模(亿元)") or r.get("剩余规模"))
        iss  = _num(r.get("issue_size_yi") or r.get("发行规模(亿元)") or r.get("发行规模"))
        prem = _num(r.get("conv_premium_pct") or r.get("溢价率"))
        mat  = str(r.get("maturity_date") or r.get("到期日期") or "")
        call_days  = _num(r.get("call_trigger_days") or r.get("强赎天计数"))
        call_price = _num(r.get("call_trigger_price") or r.get("强赎触发价"))
        put_price  = _num(r.get("put_trigger_price") or r.get("回售触发价"))
        adj_price  = _num(r.get("adjust_trigger_price") or r.get("下修触发价"))

        if cp > 0 and sp > 0:
            cv = 100.0 / cp * sp
            df.at[idx, "conv_value_100"] = round(cv, 4)
            df.at[idx, "conv_rate_per_100"] = round(100.0 / cp, 6)
            if bp > 0:
                p_calc = (bp / cv - 1.0) * 100
                df.at[idx, "conv_premium_pct_calc"] = round(p_calc, 4)
                df.at[idx, "conv_value_ratio_pct"] = round(cv / bp * 100, 4)
                use_prem = prem if pd.notna(prem) and -1e4 < prem < 1e4 else p_calc
                df.at[idx, "dual_low_calc"] = round(bp + use_prem, 4)
                pb = _num(r.get("pure_bond_value"))
                if pb and pb > 0:
                    bond_prem = (bp - pb) / pb * 100
                    df.at[idx, "premium_decomp_bond_pct"] = round(bond_prem, 4)
                    df.at[idx, "premium_decomp_conv_pct"] = round(use_prem - bond_prem, 4)

        if len(mat) >= 8:
            try:
                md = mat.replace("/","-").replace(".","-").split(" ")[0]
                if md.find("-") > 0:
                    parts = md.split("-"); y,m,d = int(parts[0]), int(parts[1]), int(parts[2])
                else:
                    y,m,d = int(md[:4]), int(md[4:6]), int(md[6:8])
                md_dt = dt.date(y,m,d)
                days = (md_dt - today).days
                df.at[idx, "maturity_days_left"] = days
                df.at[idx, "maturity_years_left"] = round(days / 365.25, 4)
                mp = _num(r.get("maturity_price") or r.get("到期赎回价"))
                coupon = _num(r.get("coupon_pct") or r.get("票面利率"))
                yrs = df.at[idx, "maturity_years_left"]
                if bp > 0 and yrs > 0:
                    ytm_given = _num(r.get("ytm_before_tax_pct") or r.get("到期税前收益"))  # jsl30只里有
                    if pd.notna(coupon):
                        coupons_sum = coupon * yrs
                    else:
                        coupon = 2.5
                        coupons_sum = coupon * yrs
                    if mp and mp > 0:
                        total_ret = (mp + coupons_sum) / bp
                    else:
                        # 到期赎回价主表没给就取行业均值：一般105~115元，默认112元（99%以上可转债票面到期赎回112左右）
                        mp = 112.0
                        total_ret = (mp + coupons_sum) / bp
                    try:
                        ytm_calc = (math.pow(total_ret, 1 / yrs) - 1) * 100
                        use_ytm = ytm_given if pd.notna(ytm_given) else ytm_calc
                        df.at[idx, "ytm_before_tax_calc"] = round(use_ytm, 4)
                        df.at[idx, "redemption_gain_pct"] = round((total_ret - 1) * 100, 4)
                        df.at[idx, "coupon_sum_estimate"] = round(coupons_sum, 4)
                        df.at[idx, "maturity_price"] = float(mp)
                    except Exception: pass
            except Exception: pass

        if sp > 0:
            if call_price > 0:
                df.at[idx, "call_trigger_pct"] = round((sp / call_price - 1) * 100, 4)
            if put_price > 0:
                df.at[idx, "put_trigger_pct"] = round((sp / put_price - 1) * 100, 4)
            if adj_price > 0:
                df.at[idx, "adjust_trigger_pct"] = round((sp / adj_price - 1) * 100, 4)
        # call_days 原始可能是 "20/15 | 30" 这种（已满足20天 / 15天阈值 = 触发条件 130%，或 30天 备选）
        raw = str(r.get("强赎天计数") or r.get("call_trigger_days") or "").strip()
        ratio = None
        if "/" in raw:
            import re as _re
            nums = [int(x) for x in _re.findall(r"\d+", raw.split("|")[0])]
            if len(nums) >= 2 and nums[1] > 0:
                ratio = round(nums[0] / nums[1] * 100, 4)
        if ratio is not None:
            df.at[idx, "call_progress_pct"] = ratio
        elif pd.notna(call_days) and call_days >= 0:
            df.at[idx, "call_progress_pct"] = round(call_days / CALL_TRIGGER_NORM_DAYS * 100, 4)

        hi = _num(r.get("high")); lo = _num(r.get("low")); prc = _num(r.get("pre_close"))
        if prc and prc>0 and hi and lo:
            df.at[idx, "daily_amplitude_calc"] = round((hi - lo) / prc * 100, 4)
        amt = _num(r.get("amount_yuan") or r.get("成交额"))
        if amt and rem and rem > 0:
            df.at[idx, "turnover_rate_calc"] = round(amt / (rem * 1e8) * 100, 6)

        if iss and iss > 0 and rem:
            df.at[idx, "remaining_ratio"] = round(rem / iss, 6)

    if "dual_low_calc" in df.columns:
        df["dual_low_rank"] = df["dual_low_calc"].rank(method="min", ascending=True, na_option="bottom").astype("Int64")
    return df


# cb_full_metrics 二级缓存：
#   STATIC_TTL：akshare 主表/条款/评级等慢变化字段，默认 300s；
#   rt TTL：由 PyTDX _cb_fetch_rt_pytdx 的 max_age=3 秒级缓存控制；
#   前端 setInterval 1s/3s/5s 轮询时，只命中静态缓存 + PyTDX 内存缓存，不再打 akshare。
CB_FULL_STATIC_KEY = "cb_full_static_snapshot_v1"
CB_FULL_STATIC_TTL_DEFAULT = None  # None → 用 Config.AK_CACHE_TTL


def _cb_full_static_snapshot(ttl=None):
    """拉取“静态字段快照”（不含 PyTDX 行情及其二次派生）。结果带内存/磁盘缓存。"""
    import time as _tt
    ttl = ttl or Config.AK_CACHE_TTL
    cache_key = CB_FULL_STATIC_KEY
    cached = _cache_get_inner(cache_key, ttl)
    if isinstance(cached, pd.DataFrame) and len(cached) > 0:
        logging.getLogger(__name__).debug("[cb_full_static] cache hit rows=%d", len(cached))
        return cached.copy()
    t0 = _tt.time()
    # --- 以下是原 cb_full_metrics 前半段（去掉末尾 PyTDX 合并 + 二次 _add_derived）---
    df_main = _cached("cb_full_main", ttl, ak.bond_cb_redeem_jsl)
    df_main = df_main.copy()
    if "代码" in df_main.columns:
        df_main["bond_code"] = df_main["代码"].astype(str).map(_norm_code)
    try:
        df_spot = _cached("cb_full_spot", ttl, ak.bond_zh_hs_cov_spot)
        df_spot = df_spot.copy()
        col_code = next((c for c in ("代码","code") if c in df_spot.columns), None)
        if col_code:
            df_spot["bond_code"] = df_spot[col_code].astype(str).map(_norm_code)
            df = df_main.merge(df_spot, on="bond_code", how="left", suffixes=("", "_spot"))
        else:
            df = df_main
    except Exception:
        df = df_main
    try:
        df_jsl = _cached("cb_full_jsl", ttl, ak.bond_cb_jsl)
        df_jsl = df_jsl.copy()
        col_code = next((c for c in ("代码","转债代码","code") if c in df_jsl.columns), None)
        if col_code:
            df_jsl["bond_code"] = df_jsl[col_code].astype(str).map(_norm_code)
            df = df.merge(df_jsl, on="bond_code", how="left", suffixes=("", "_jsl"))
    except Exception:
        pass
    # RPT_BOND_CB_LIST / ths+cov_info 兜底
    _rpt_ok = False
    try:
        df, _rpt_matched = _cb_merge_rpt_bond_cb_list(df)
        _rpt_ok = _rpt_matched >= 300
        logging.getLogger(__name__).info("[方案B-RPT一次性] matched=%d rpt_ok=%s", _rpt_matched, _rpt_ok)
    except Exception as e:
        logging.getLogger(__name__).warning("_cb_merge_rpt_bond_cb_list fail: %s", e)
        _rpt_ok = False
    if not _rpt_ok:
        try:
            df = _cb_merge_ths_info(df)
        except Exception as e:
            logging.getLogger(__name__).warning("_cb_merge_ths_info fail: %s", e)
        df = _apply_std_columns(df)
        try:
            df = _cb_merge_cov_info(df)
        except Exception as e:
            logging.getLogger(__name__).warning("_cb_merge_cov_info fail: %s", e)
    else:
        df = _apply_std_columns(df)
    try:
        df = _cb_merge_issuer_rating(df)
    except Exception as e:
        logging.getLogger(__name__).warning("_cb_merge_issuer_rating fail: %s", e)
    # 首次派生（给 PyTDX 合并前打底，非实时价）
    df = _add_derived(df)
    logging.getLogger(__name__).info("[cb_full_static] built rows=%d t=%dms", len(df), int((_tt.time()-t0)*1000))
    _cache_put_inner(cache_key, ttl, df.copy())
    return df.copy()


def cb_full_metrics(ttl = None, static_ttl = None):
    """全指标：静态字段走长TTL（static_ttl），PyTDX实时行情走秒级缓存。
    ttl 参数仅用于 PyTDX、QMT 兜底等动态部分（内部各自 max_age）。
    """
    import time as _tt
    static_ttl = static_ttl if isinstance(static_ttl, int) and static_ttl >= 0 else CB_FULL_STATIC_TTL_DEFAULT
    if static_ttl is None: static_ttl = Config.AK_CACHE_TTL
    # ============== 并发编排：静态快照 与 PyTDX 实时抓取 并行 ==============
    # 静态快照从（可能过期的）缓存取出代码 universe 后，PyTDX 即可提前启动，
    # 与 akshare 重建并行，省去原本串行的 ~2.7s 静态耗时。
    _f_static = _cb_par_exec.submit(_cb_full_static_snapshot, ttl=static_ttl)
    _rt_fut = None
    _codes = _cb_static_codes_cached()
    if _codes is not None and (_codes[0] or _codes[1]):
        _rt_fut = _cb_par_exec.submit(_cb_fetch_rt_pytdx_split, _codes[0], _codes[1])
    df = _f_static.result()
    if _rt_fut is not None:
        _rt = _rt_fut.result()
    else:
        _bc = df["bond_code"].astype(str).tolist() if "bond_code" in df.columns else []
        _sc = df["stock_code"].astype(str).tolist() if "stock_code" in df.columns else []
        _rt = _cb_fetch_rt_pytdx_split(_bc, _sc)
    # ============== 实时行情全列（转债/正股 价格/涨跌幅/振幅/量/额） ==============
    # PyTDX 第一优先级（通达信原生协议，3秒轮询不封IP，与 /kline 同源）
    # QMT HTTP 桥 v3 /cb_rt_snapshot 兜底：仅补充 PyTDX 未覆盖的空白
    # 备用：58600 RPC（交易时段 getMarketData 非空）
    try:
        df = _cb_merge_rt_pytdx(df, rt=_rt)          # PyTDX 第一：全量覆盖实时列
    except Exception as e:
        logging.getLogger(__name__).warning("_cb_merge_rt_pytdx fail: %s", e)
    # 覆盖率是否达标：PyTDX 命中且转债覆盖≥90% 则信任 PyTDX；否则 QMT 桥兜底补空白
    _pytdx_meta = (df.attrs or {}).get("__pytdx_meta__") or {}
    _pytdx_hit = _pytdx_meta.get("ok") is True and (
        _pytdx_meta.get("bonds_chg_ok", 0) > 0 or _pytdx_meta.get("stocks_chg_ok", 0) > 0)
    _pytdx_bond_ratio = 0.0
    if _pytdx_hit and int(_pytdx_meta.get("distinct_bonds") or 0) > 0:
        _pytdx_bond_ratio = _pytdx_meta.get("bonds_chg_ok", 0) / _pytdx_meta.get("distinct_bonds", 1)
    if not _pytdx_hit or _pytdx_bond_ratio < 0.90:
        if _pytdx_hit:
            logging.getLogger(__name__).info("[rt] PyTDX 覆盖 %.0f%%<90%%，QMT 桥兜底补齐空白", _pytdx_bond_ratio * 100)
        try:
            df = _cb_merge_rt_qmt_bridge(df, overwrite_if_filled=False)   # 仅补 PyTDX 空白
        except Exception as e:
            logging.getLogger(__name__).warning("_cb_merge_rt_qmt_bridge fail: %s", e)
            try:
                from core import qmt_detail_service as qsvc
                if hasattr(qsvc, "augment_rt_codes"):
                    rt_qmt = qsvc.augment_rt_codes(df["bond_code"].astype(str).tolist())
                    df = _cb_merge_rt_qmt_fallback(df, rt_qmt)
            except Exception as e2:
                logging.getLogger(__name__).warning("QMT RT fallback fail: %s", e2)
    # 行情合并后重算派生列：用新价格重算 溢价率/双低/YTM/转股价值/换手率
    # 先抓取实时来源元信息：_cb_merge_rt_* 把 __rt_src_meta__ / __pytdx_meta__ 写入 df.attrs，
    # 但随后 _add_derived / _merge_qmt 内部的 merge/filter 会丢弃 attrs，故此处先缓存。
    _rt_src_meta = dict((df.attrs or {}).get("__rt_src_meta__") or {})
    _pytdx_meta_final = dict((df.attrs or {}).get("__pytdx_meta__") or {})
    df = _add_derived(df)
    df = _merge_qmt(df)
    # 还原实时来源元信息：确保前端 /cb/full 的 qmt.meta 能拿到 _src 与覆盖率
    _final_rt = {}
    _final_rt.update(_rt_src_meta)
    _final_rt.update(_pytdx_meta_final)
    if _pytdx_hit:
        _final_rt["_src"] = "pytdx_first"
        _final_rt["_pytdx_bond_ratio"] = round(_pytdx_bond_ratio, 4)
    if _final_rt:
        df.attrs["__rt_src_meta__"] = _final_rt
    return df


# ============================================================================
# QMT 本地增强：合并 qmt_detail_service 输出
# ============================================================================
def _merge_qmt(df: pd.DataFrame) -> pd.DataFrame:
    """对 akshare 已经标准列化 / 派生计算过的表，再用 QMT 侧补空缺字段。

    合并策略：
      1) 核心列（QMT_CORE_MERGE_COLS）：ak 为空才用 qmt 填；
      2) 诊断列（_qmt / _yi_qmt 后缀）：总是保留追加；
      3) ak 非空 但 qmt 也给出同一字段（如 rating），绝不覆盖 ak。
    统计（挂在 df.attrs 上）：
      __qmt_filled_counts__ = {col: {"ak_non_empty": N, "qmt_filled_ak_blank": M}}
      __qmt_diag_counts__ = {diag_col: N_non_empty}
    """
    if df is None or len(df) == 0:
        return df
    try:
        from core import qmt_detail_service as qsvc
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("QMT 模块不可用，跳过合并：%s", e)
        df.attrs["__qmt_meta__"] = {"via": "none", "reason": "import fail: %s" % e}
        return df

    codes = [c for c in df["bond_code"].astype(str).tolist() if c]
    if not codes:
        df.attrs["__qmt_meta__"] = {"via": "none", "reason": "no bond_code in df"}
        return df

    qmt_result = qsvc.augment_codes(codes)
    qmt_meta = qmt_result.pop("__meta__", {})
    via = qmt_meta.get("via", "none")

    fills = {c: {} for c in codes}
    diags = {c: {} for c in codes}
    filled_counts = {col: {"ak_non_empty": 0, "qmt_filled_ak_blank": 0}
                     for col in QMT_CORE_MERGE_COLS}

    # 第一步：预计算 ak 侧非空数量
    for col in QMT_CORE_MERGE_COLS:
        if col not in df.columns:
            continue
        s = df[col]
        try:
            non_empty = int(((~s.isna()) & (s.astype(str).str.strip().isin(
                {"", "-", "--", "\u2014"}) == False)).sum())
        except Exception:
            non_empty = int((~s.isna()).sum())
        filled_counts[col]["ak_non_empty"] = non_empty

    # 第二步：组装 fills / diags
    for c in codes:
        row = qmt_result.get(c) or {}
        raw_keys = row.get("__qmt_raw_keys", [])
        has_data = len(row) > 1 and len(raw_keys) > 0

        for col in QMT_CORE_MERGE_COLS:
            v = row.get(col)
            if v is None or (isinstance(v, float) and pd.isna(v)):
                continue
            if isinstance(v, str) and v.strip() == "":
                continue
            fills[c][col] = v
        for k, v in row.items():
            if k.startswith("__"):
                continue
            if isinstance(v, float) and pd.isna(v):
                continue
            if any(k.endswith(suf) for suf in QMT_DIAG_COLS_SUFFIXES):
                diags[c][k] = v
        if via in ("none",):
            diags[c]["qmt_via"] = "none"
        elif not has_data:
            diags[c]["qmt_via"] = via + "_miss"
        else:
            diags[c]["qmt_via"] = via
        diags[c]["qmt_n_raw_fields"] = len(raw_keys)

    rows_merge = []
    codes_in_df = [str(c) for c in df["bond_code"].tolist()]
    for c in codes_in_df:
        merged = dict(fills.get(c, {}))
        merged.update(diags.get(c, {}))
        merged["bond_code"] = c
        rows_merge.append(merged)
    df_merge = pd.DataFrame(rows_merge)

    existing_cols = set(df.columns)
    diag_cols_to_join = [c for c in df_merge.columns
                         if c == "bond_code" or c not in QMT_CORE_MERGE_COLS]
    if len(diag_cols_to_join) > 1:
        df = df.merge(df_merge[diag_cols_to_join], on="bond_code",
                      how="left", suffixes=("", "__qmt_diag"))

    # 第四步：按规则填核心列，累计 qmt_filled_ak_blank
    for col in QMT_CORE_MERGE_COLS:
        if col not in df_merge.columns:
            continue
        if col not in df.columns:
            df[col] = df_merge[col]
            try:
                qmt_non = int(
                    (~df_merge[col].isna()
                     & df_merge[col].map(lambda x: not (
                         isinstance(x, str) and x.strip() == ""))).sum()
                )
            except Exception:
                qmt_non = 0
            filled_counts[col]["qmt_filled_ak_blank"] = qmt_non
            continue
        arr_ak = df[col].values
        arr_qmt = df_merge[col].values
        actual_fill = 0
        for i in range(len(arr_ak)):
            a = arr_ak[i] if i < len(arr_ak) else None
            ak_empty = (
                a is None
                or (isinstance(a, float) and pd.isna(a))
                or (isinstance(a, str) and a.strip() in ("", "-", "--", "\u2014"))
            )
            if not ak_empty:
                continue
            q = arr_qmt[i] if i < len(arr_qmt) else None
            q_nonempty = not (
                q is None
                or (isinstance(q, float) and pd.isna(q))
                or (isinstance(q, str) and q.strip() == "")
            )
            if q_nonempty:
                arr_ak[i] = q
                actual_fill += 1
        df[col] = arr_ak
        filled_counts[col]["qmt_filled_ak_blank"] = actual_fill

    # 第五步：诊断列非空统计
    diag_count = {}
    want_diag_cols = [c for c in df.columns
                      if any(c.endswith(suf) for suf in QMT_DIAG_COLS_SUFFIXES)
                      or c in ("qmt_via", "qmt_n_raw_fields")]
    for dc in want_diag_cols:
        s = df[dc]
        try:
            neq = int(((~s.isna()) &
                      (~s.astype(str).str.strip().isin({"", "nan", "None"}))).sum())
        except Exception:
            neq = int((~s.isna()).sum())
        diag_count[dc] = neq

    df.attrs["__qmt_meta__"] = qmt_meta
    df.attrs["__qmt_filled_counts__"] = filled_counts
    df.attrs["__qmt_diag_counts__"] = diag_count
    return df

