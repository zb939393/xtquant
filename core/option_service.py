# -*- coding: utf-8 -*-
"""期权监控数据服务：上交所(SSE) / 深交所(SZSE) ETF 期权。

数据源（已验证可用）：
  - 行情：新浪 hq.sinajs.cn  `CON_OP_{code}`  —— SSE 与 SZSE 共用同一 51 字段格式。
      关键字段：2=最新价 5=持仓量 6=涨幅% 7=行权价 8=昨收 36=标的代码
               37=合约简称 38=振幅 39=最高 40=最低 41=成交量
               45=C/P 46=到期日 47=剩余天数 49=内在价值 50=时间价值
  - 合约代码：
      SSE：akshare option_sse_list_sina 取月份 → 新浪 OP_UP_/OP_DOWN_{标的}{yymm} 取合约。
      SZSE：akshare option_current_day_szse() 取全部合约（akshare 无 SZ 行情，仅能取代码）。
  - 标的价格：SSE `sh{code}` / SZSE `sz{code}`。
  - 指标：Delta/Gamma/Theta/Vega 由 Black-Scholes 自算（隐含波动率由期权价格牛顿法反解）；
         内在价值/时间价值/虚实度/溢价率/杠杆比率/实际杠杆 由闭式公式解析。

说明：akshare 对 SZSE 期权没有实时行情，但能取全部合约代码；合约行情通过新浪 CON_OP_ 统一拉取，
因此 SSE/SZSE 走同一条行情通路。
"""
import re
import math
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import akshare as ak
from config import Config

from core.ak_service import (
    _cache_get_inner,
    _cache_put_inner,
    _date_norm,
    _num,
)

# 新浪行情请求头（无 Referer 会 403）
_OPT_SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://stock.finance.sina.com.cn/",
}
# SSE ETF 期权标的：代码 / 名称 / option_sse_list_sina 的 symbol 参数
# 588000（科创50ETF）与 588080（科创50ETF易方达）均以自身代码作为 symbol 取月份列表。
_OPT_SSE_UNDERLYINGS = [
    ("510050", "上证50ETF", "50ETF"),
    ("510300", "沪深300ETF", "300ETF"),
    ("510500", "中证500ETF", "500ETF"),
    ("588000", "科创50ETF", "588000"),
    ("588080", "科创50ETF易方达", "588080"),
]


def _opt_sina_get(symbols_csv):
    """批量拉新浪行情，返回 gbk 解码文本。symbols_csv 形如 'sh510050,CON_OP_1001...,'。"""
    import requests as _reqs
    sess = _reqs.Session()
    sess.trust_env = False
    sess.headers.update(_OPT_SINA_HEADERS)
    try:
        r = sess.get("https://hq.sinajs.cn/list=" + symbols_csv, timeout=12)
        return r.content.decode("gbk", "ignore")
    except Exception:
        return ""


def _opt_con_op_parse(parts):
    """解析 CON_OP_ 的 51 字段串 → dict。parts: list[str]。"""
    if not parts or len(parts) < 48:
        return None
    def f(i):
        return _num(parts[i]) if i < len(parts) else float("nan")
    return {
        "last": f(2), "bid": f(1), "ask": f(3), "oi": f(5),
        "change_pct": f(6), "strike": f(7), "preclose": f(8),
        "open": f(9), "name": parts[37] if len(parts) > 37 else "",
        "amplitude": f(38), "high": f(39), "low": f(40),
        "volume": f(41), "amount": f(42),
        "underlying_code": parts[36] if len(parts) > 36 else "",
        "type": parts[45] if len(parts) > 45 else "",
        "expiry": _date_norm(parts[46]) if len(parts) > 46 else "",
        "days": int(f(47)) if f(47) == f(47) else 0,
        "intrinsic_sina": f(49), "time_value_sina": f(50),
    }


def trading_minutes_since_open(now=None):
    """A 股交易时段内自 09:30 开盘以来「已成交的分钟数」（剔除 11:30-13:00 午休）。

    用于「活跃度 = 成交额 / 已交易分钟」，估算平均每分钟成交额。
    盘前返回 1.0、盘后返回 240.0（整天可交易时长），避免除零。
    """
    import datetime as _dt
    if now is None:
        now = _dt.datetime.now()
    am_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    am_end = now.replace(hour=11, minute=30, second=0, microsecond=0)
    pm_start = now.replace(hour=13, minute=0, second=0, microsecond=0)
    pm_end = now.replace(hour=15, minute=0, second=0, microsecond=0)
    if now < am_start:
        return 1.0
    if now > pm_end:
        return 240.0
    if now >= am_end:
        minutes = 120.0
        if now >= pm_start:
            minutes += (now - pm_start).total_seconds() / 60.0
    else:
        minutes = (now - am_start).total_seconds() / 60.0
    return max(minutes, 1.0)


# ============================================================================
# 合约列表（静态，缓存 1 小时）
# ============================================================================
def option_contracts(force=False):
    """返回统一的期权合约列表（DataFrame）。列见各交易所分支。

    缓存策略：日内复用 + 每日盘前失效。合约静态信息按自然日缓存，跨天后首次访问
    自动重抓（保证每月 rollover 的新合约及时生效）；盘中（含 Flask 重启）命中磁盘
    缓存即时返回，避免每次冷启动都重新发现合约（约 4.7s）。
    """
    from datetime import date as _date
    key = "opt_contracts"
    today = _date.today().isoformat()
    if not force:
        cached = _cache_get_inner(key, Config.OPTION_STATIC_TTL)
        if isinstance(cached, dict) and cached.get("date") == today:
            return cached["df"]

    # ---- 各标的仅取一次月份列表（去重 + 并发）----
    uniq_syms = []
    for _, _, sym in _OPT_SSE_UNDERLYINGS:
        if sym not in uniq_syms:
            uniq_syms.append(sym)
    months_cache = {}
    if uniq_syms:
        with ThreadPoolExecutor(max_workers=len(uniq_syms)) as ex:
            fut_by_sym = {ex.submit(ak.option_sse_list_sina, symbol=s): s for s in uniq_syms}
            for f, s in fut_by_sym.items():
                try:
                    months_cache[s] = list(f.result() or [])
                except Exception:
                    months_cache[s] = []

    # 构造新浪发现任务：(标的code, 名称, 方向前缀, yymm4, 类型C/P)
    disc_tasks = []
    for code, name, sym in _OPT_SSE_UNDERLYINGS:
        for ym in months_cache.get(sym, []):
            ym4 = str(ym)[-4:]
            for kind, t in (("OP_UP_", "C"), ("OP_DOWN_", "P")):
                disc_tasks.append((code, name, kind, ym4, t))

    def _run_disc(task):
        code, name, kind, ym4, t = task
        try:
            txt = _opt_sina_get(kind + code + ym4)
        except Exception:
            return []
        return [{
            "code": c, "exchange": "SSE", "underlying_code": code,
            "underlying_name": name, "type": t, "market_prefix": "sh",
        } for c in re.findall(r"CON_OP_(\d{8})", txt)]

    def _sz_rows():
        try:
            sz = ak.option_current_day_szse()
        except Exception:
            return []
        if sz is None or not len(sz):
            return []
        out = []
        for _, r in sz.iterrows():
            raw = str(r.get("标的证券简称(代码)", ""))
            m = re.search(r"\((\d{6})\)", raw)
            underlying_code = m.group(1) if m else ""
            underlying_name = raw.split("(")[0].strip()
            tcn = str(r.get("合约类型", ""))
            t = "C" if tcn == "认购" else ("P" if tcn == "认沽" else "")
            out.append({
                "code": str(r.get("合约编码", "")),
                "exchange": "SZSE",
                "underlying_code": underlying_code,
                "underlying_name": underlying_name,
                "type": t, "market_prefix": "sz",
                "contract_code": str(r.get("合约代码", "")),
                "contract_name": str(r.get("合约简称", "")),
                "strike": _num(r.get("行权价")),
                "expiry": _date_norm(r.get("到期日")),
                "days": int(_num(r.get("合约到期剩余交易天数"))),
            })
        return out

    rows = []
    # SSE 合约发现（并发）与 SZSE 取数（并发）同时进行，避免串行等待
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(disc_tasks) + 1))) as ex:
        f_sz = ex.submit(_sz_rows)
        for res in ex.map(_run_disc, disc_tasks):
            rows.extend(res)
        rows.extend(f_sz.result())

    df = pd.DataFrame(rows)
    _cache_put_inner(key, Config.OPTION_STATIC_TTL, {"df": df, "date": today})
    return df


def _option_fetch_quotes(codes):
    """批量拉 CON_OP_ 行情 → {code: parts(list[str])}。每批 ≤150 个。"""
    out = {}
    if not codes:
        return out
    for i in range(0, len(codes), 150):
        chunk = codes[i:i + 150]
        symbols = ",".join("CON_OP_" + c for c in chunk)
        txt = _opt_sina_get(symbols)
        if not txt:
            continue
        for m in re.finditer(r'hq_str_CON_OP_(\d{8})="([^"]*)"', txt):
            parts = m.group(2).split(",")
            if len(parts) >= 48:
                out[m.group(1)] = parts
    return out


def _option_fetch_underlying(pairs):
    """pairs: [(market_prefix, underlying_code)] → {underlying_code: price}。"""
    out = {}
    uniq = list({(p, c) for p, c in pairs})
    if not uniq:
        return out
    symbols = ",".join(p + c for p, c in uniq)
    txt = _opt_sina_get(symbols)
    if not txt:
        return out
    for p, c in uniq:
        m = re.search(r'hq_str_%s%s="([^"]*)"' % (p, c), txt)
        if not m:
            continue
        parts = m.group(1).split(",")
        # 新浪 ETF/股票行情：index 3 = 当前价
        price = _num(parts[3]) if len(parts) > 3 else float("nan")
        if price == price and price > 0:
            out[c] = price
    return out


# ============================================================================
# Black-Scholes
# ============================================================================
def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(S, K, T, r, sigma, is_call):
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return max(0.0, S - K) if is_call else max(0.0, K - S)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    if is_call:
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def _bs_greeks(S, K, T, r, sigma, is_call):
    """返回 (delta, gamma, vega_per_1%, theta_per_day)。退化情形返回 (阶跃,0,0,0)。"""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        d = 1.0 if (is_call and S > K) else (0.0 if is_call else (-1.0 if S < K else 0.0))
        return (d, 0.0, 0.0, 0.0)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    nd1 = _ncdf(d1)
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    delta = nd1 if is_call else nd1 - 1.0
    gamma = pdf / (S * sigma * sqrtT)
    vega = S * pdf * sqrtT / 100.0
    if is_call:
        theta = (-(S * pdf * sigma) / (2.0 * sqrtT) - r * K * math.exp(-r * T) * _ncdf(d2)) / 365.0
    else:
        theta = (-(S * pdf * sigma) / (2.0 * sqrtT) + r * K * math.exp(-r * T) * (1.0 - _ncdf(d2))) / 365.0
    return (delta, gamma, vega, theta)


def _bs_implied_vol(price, S, K, T, r, is_call, default):
    """牛顿法反解隐含波动率；无时间价值或退化时回退 default。"""
    if price is None or T <= 0 or S <= 0 or K <= 0:
        return default
    intrinsic = max(0.0, S - K) if is_call else max(0.0, K - S)
    if price <= intrinsic + 1e-9:
        return default
    sigma = default
    for _ in range(60):
        if sigma <= 0:
            sigma = 1e-3
        diff = _bs_price(S, K, T, r, sigma, is_call) - price
        if abs(diff) < 1e-7:
            break
        sqrtT = math.sqrt(T)
        d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
        pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
        vega = S * pdf * sqrtT
        if vega < 1e-9:
            break
        sigma = sigma - diff / vega
        if sigma <= 0:
            sigma = 1e-3
    return max(1e-3, min(sigma, 5.0))


# ============================================================================
# 全指标装配
# ============================================================================
def _option_build_full_df():
    contracts = option_contracts()
    if len(contracts) == 0:
        return pd.DataFrame()
    codes = contracts["code"].astype(str).tolist()
    quotes = _option_fetch_quotes(codes)

    # 标的价格（按 underlying_code 去重拉取）
    pairs = [(str(c.get("market_prefix", "")), str(c.get("underlying_code", "")))
             for _, c in contracts.iterrows()]
    underlying_prices = _option_fetch_underlying(pairs)

    # 标的名称回查（SSE 自带 / SZSE 来自合约静态）
    underlying_name_map = {}
    for _, c in contracts.iterrows():
        uc = str(c.get("underlying_code", ""))
        nm = str(c.get("underlying_name", ""))
        if uc and nm:
            underlying_name_map[uc] = nm

    r = Config.OPTION_RISK_FREE_RATE
    vdef = Config.OPTION_VOL_DEFAULT
    minutes = trading_minutes_since_open()
    records = []

    for _, c in contracts.iterrows():
        code = str(c.get("code", ""))
        _cc = c.get("contract_code", "")
        _cc = "" if (_cc != _cc) else str(_cc)
        if _cc.lower() == "nan":
            _cc = ""
        rec = {
            "code": code,
            "exchange": str(c.get("exchange", "")),
            "underlying_code": str(c.get("underlying_code", "")),
            "underlying_name": str(c.get("underlying_name", "")),
            "type": str(c.get("type", "")),
            "contract_name": str(c.get("contract_name", "")),
            "contract_code": _cc,
            "strike": _num(c.get("strike")) if _num(c.get("strike")) == _num(c.get("strike")) else float("nan"),
            "expiry": str(c.get("expiry", "")),
            "days": int(_num(c.get("days"))) if _num(c.get("days")) == _num(c.get("days")) else 0,
        }
        q = quotes.get(code)
        if q:
            p = _opt_con_op_parse(q)
            if p:
                rec.update({
                    "last": p["last"], "preclose": p["preclose"],
                    "change_pct": p["change_pct"], "bid": p["bid"], "ask": p["ask"],
                    "volume": p["volume"], "oi": p["oi"], "high": p["high"],
                    "low": p["low"], "amplitude": p["amplitude"],
                    "amount": p["amount"],
                    "activity": (p["volume"] / minutes) if (minutes > 0 and p["volume"] == p["volume"]) else float("nan"),
                    "strike": p["strike"] if p["strike"] == p["strike"] else rec["strike"],
                    "expiry": p["expiry"] or rec["expiry"],
                    "days": p["days"] or rec["days"],
                    "type": p["type"] or rec["type"],
                    "contract_name": p["name"] or rec["contract_name"],
                    "underlying_code": p["underlying_code"] or rec["underlying_code"],
                })
        uc = rec["underlying_code"]
        if uc and uc in underlying_name_map:
            rec["underlying_name"] = underlying_name_map[uc]

        S = underlying_prices.get(uc, float("nan"))
        K = rec["strike"]
        price = rec.get("last", float("nan"))
        is_call = (rec["type"] == "C")
        T = max(rec["days"], 0) / 365.0

        if S == S and K == K and price == price and price > 0:
            intrinsic = max(0.0, S - K) if is_call else max(0.0, K - S)
            time_value = price - intrinsic
            moneyness = ((S - K) / K) if is_call else ((K - S) / K)
            if S > 0:
                premium_rate = (price + K - S) / S if is_call else (price + S - K) / S
                leverage_ratio = S / price
            else:
                premium_rate = float("nan")
                leverage_ratio = float("nan")
            sigma = _bs_implied_vol(price, S, K, T, r, is_call, vdef)
            delta, gamma, vega, theta = _bs_greeks(S, K, T, r, sigma, is_call)
            effective_leverage = leverage_ratio * delta if leverage_ratio == leverage_ratio else float("nan")
            rec.update({
                "underlying_price": S,
                "intrinsic": intrinsic, "time_value": time_value,
                "moneyness": moneyness, "premium_rate": premium_rate,
                "leverage_ratio": leverage_ratio, "iv": sigma,
                "delta": delta, "gamma": gamma, "vega": vega, "theta": theta,
                "effective_leverage": effective_leverage,
            })
        else:
            rec.update({
                "underlying_price": S,
                "intrinsic": float("nan"), "time_value": float("nan"),
                "moneyness": float("nan"), "premium_rate": float("nan"),
                "leverage_ratio": float("nan"), "iv": float("nan"),
                "delta": float("nan"), "gamma": float("nan"), "vega": float("nan"),
                "theta": float("nan"), "effective_leverage": float("nan"),
                "amount": float("nan"), "activity": float("nan"),
            })
        records.append(rec)

    df = pd.DataFrame(records)
    return df


_OPT_FULL_CACHE = {"df": None, "ts": 0.0}


def option_full_df(force=False):
    """期权全指标 DataFrame（实时）。OPTION_RT_TTL 内存缓存 + 磁盘兜底。"""
    if force:
        df = _option_build_full_df()
        _OPT_FULL_CACHE["df"] = df
        _OPT_FULL_CACHE["ts"] = time.time()
        return df
    cached_mem = _OPT_FULL_CACHE.get("df")
    if cached_mem is not None and (time.time() - _OPT_FULL_CACHE.get("ts", 0)) < Config.OPTION_RT_TTL:
        return cached_mem
    cached_disk = _cache_get_inner("opt_full_df", Config.OPTION_RT_TTL)
    if cached_disk is not None:
        _OPT_FULL_CACHE["df"] = cached_disk
        _OPT_FULL_CACHE["ts"] = time.time()
        return cached_disk
    df = _option_build_full_df()
    _OPT_FULL_CACHE["df"] = df
    _OPT_FULL_CACHE["ts"] = time.time()
    _cache_put_inner("opt_full_df", Config.OPTION_RT_TTL, df)
    return df
