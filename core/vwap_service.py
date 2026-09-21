# -*- coding: utf-8 -*-
"""VWAP 偏离度日内策略 · 实时看盘服务（对齐 futures/vwap_full_report 口径）。

策略口径（与回测报告书 §4/§5 一致，VWAP 权重价按本数据源实测修正，见下）：
    VWAP     = cumsum(close * vol) / cumsum(vol)    # 按交易日重置
    dev      = close - VWAP
    dev_atr  = dev / ATR(5min, 14, Wilder)          # 抹平波动率，跨品种可比
    dev_z    = (dev_atr - mu[pos]) / sd[pos]        # pos = 当日第几根 1 分钟（0..239）
  做多触发   dev_z 向上突破 +k_long（上一根有效值在阈值下方、本根穿到上方）
  做空触发   dev_z 向下跌破 k_short（上一根有效值在阈值上方、本根穿到下方）
  （突破口径：仅在穿阈那一根触发，而非持续超阈期间每根都挂单）
  成交      信号在 t 根收盘确认 -> t+1 根开盘成交（无未来函数）
  交易时段  仅下午 13:00-14:55（上午 10-11 点几乎无预测力）
  止损      1.5 x ATR(5min)   止盈 1.5R   时间止损 60 根   14:55 强平

mu/sd 基线由「过去 N 个交易日、同一 pos 的 dev_atr」统计得到——全部为历史数据，零前视。

■ 两处对本数据源的实测修正（2026-09-06 用真实分时均价做金标准验证）

1) VWAP 权重价用 close，不是典型价 (H+L+C)/3。
   报告书 §3.3 因 TDX 本地 .lc1 的 amount 字段为脏数据，改用典型价代理并称
   「三种代理差异 < 0.05 点」。但在 pytdx 扩展行情接口上实测（四品种全天逐点比对
   通达信分时图的累计均价 avg_price）：
       close 加权   mean|err| = 0.0003 ~ 0.0010 点   max = 0.0027 点   <- 精确复现
       典型价加权   mean|err| = 0.59 ~ 1.44 点       max = 29.50 点
       (H+L)/2      mean|err| = 0.88 ~ 2.17 点       max = 29.50 点
       (H+C)/2      mean|err| = 0.50 ~ 3.00 点
   即本数据源的均价线口径就是 Σ(close×vol)/Σ(vol)，误差仅浮点精度量级。
   采用 close 后，看板上的 VWAP 线与通达信分时图黄线完全重合。

2) ATR 计算前剔除开盘集合竞价的跳空污染。
   实测每个交易日第 1 根 1 分钟 K 线（09:31）的振幅中位数 0.494%，是其余分钟
   （0.069%）的 7 倍——open/low 残留了昨收附近的集合竞价幽灵价。这会系统性推高
   ATR。故对 pos==0 的 bar 把 high/low 收敛到 [min(open,close), max(open,close)]
   后再算 TR（跳空属隔夜风险，日内策略本就强制不留仓，不应计入日内 ATR）。
   仅影响 ATR 输入；VWAP 用 close 加权，不受此污染影响。

数据源：core.futures_service（通达信扩展行情 47#，真实行情，无合成数据）。
"""
import os
import time
import threading
import pickle
from datetime import datetime, timedelta

from core import futures_service as fut

# ============================ 常量 / 默认参数 ============================

# 一个交易日 240 根 1 分钟：09:31~11:30（120 根）+ 13:01~15:00（120 根）
BARS_PER_DAY = 240
AM_END_POS = 120          # 上午段占前 120 根
WARMUP_BARS = 30          # 每日前若干根不交易（VWAP/ATR 未稳定）

DEFAULT_PARAMS = {
    "k_long": 1.0,        # 做多阈值 dev_z >= +1.0
    "k_short": -1.5,      # 做空阈值 dev_z <= -1.5
    "rr": 1.5,            # 止盈风险回报比 1.5R
    "atr_mult": 1.5,      # 止损 = 1.5 x ATR(5min)
    "atr_period": 14,     # ATR 周期
    "time_stop": 60,      # 持有满 60 根 1 分钟强制平仓
    "session_start": "13:00",   # 仅此时段内允许开新仓
    "session_end": "14:55",     # 14:55 强制平仓，绝不过夜
    "baseline_days": 30,  # 基线窗口（交易日）
}

# 上午腿（开盘驱动 drive）默认参数。
#   方向依据 ret1545 = (9:45 收 ÷ 9:30 开 − 1) × 100%，全品种统一门槛 k_pct（默认 40bp）。
#   入场 09:46 开盘；止损 = 1.5 × ATR(5min)（报告 §②③ 两腿共用同一止损基准：IFL9 ATR≈8.5 → 12.76 点；
#       报告正文把该 ATR 标作"1 分钟 ATR 8.51"，但数值等于 5 分钟口径，故实现统一用 5min ATR）。
#   止盈 = 2.0R（rr=2）；时间止损到 11:30（≈105 根）尾盘强平，绝不过夜。
#   与下午腿不同：阈值对称、持仓窗口在上午；止损 ATR 口径与下午腿一致（5min）。
AM_DEFAULT_PARAMS = {
    "k_pct": 0.40,        # ret1545 门槛（%），≥ +k_pct 做多 / ≤ −k_pct 做空
    "rr": 2.0,            # 止盈风险回报比 2.0R
    "atr_mult": 1.5,      # 止损 = 1.5 x ATR(5min)
    "atr_period": 14,     # ATR 周期
    "time_stop": 105,     # 持有满 105 根 1 分钟强制平仓（≈到 11:30）
    "session_start": "09:46",   # 09:46 开盘入场
    "session_end": "11:30",     # 11:30 强制平仓（上午了结，不与下午腿抢仓位）
    "baseline_days": 30,  # 基线窗口（交易日，仅下午腿 dev_z 用到）
    "atr_mode": "5min",   # 上午腿止损 ATR 与下午腿统一用 5 分钟（报告标注 8.51 实为 5min 口径）
}

# 磁盘缓存目录（1 分钟历史，用于构建 dev_z 基线）
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "vwap_cache")
_PAGE = 700               # 单次分页拉取根数（实测服务端上限 700）
_LOCK = threading.Lock()


# ============================ 工具函数 ============================

def _hms(date_str):
    """'2026-09-02 09:51' -> ('2026-09-02', '09:51')"""
    s = (date_str or "").strip()
    if " " in s:
        d, t = s.split(" ", 1)
        return d, t[:5]
    return s[:10], ""


def _pos_of(hhmm):
    """通达信口径：1 分钟 bar 时间 -> 当日第几根（0..239）；非交易时刻返回 None。

    上午 09:31 -> 0 ... 11:30 -> 119
    下午 13:01 -> 120 ... 15:00 -> 239
    """
    if not hhmm or len(hhmm) < 5:
        return None
    try:
        h = int(hhmm[:2]); m = int(hhmm[3:5])
    except Exception:
        return None
    t = h * 60 + m
    if 9 * 60 + 31 <= t <= 11 * 60 + 30:
        return t - (9 * 60 + 30) - 1
    if 13 * 60 + 1 <= t <= 15 * 60:
        return AM_END_POS + (t - 13 * 60) - 1
    return None


def _minute_of(hhmm):
    """'14:55' -> 895（自午夜分钟数）"""
    try:
        return int(hhmm[:2]) * 60 + int(hhmm[3:5])
    except Exception:
        return -1


def _sanitize_for_atr(hist):
    """ATR 输入清洗：剔除每交易日第 1 根的开盘集合竞价跳空（见模块文档修正 2）。

    仅调整 high/low（收敛到 [min(open,close), max(open,close)]），不改 open/close/vol。
    """
    out = []
    for b in hist:
        _, hm = _hms(b["date"])
        nb = dict(b)
        if _pos_of(hm) == 0:
            lo = min(b["open"], b["close"])
            hi = max(b["open"], b["close"])
            if nb["low"] < lo:
                nb["low"] = lo
            if nb["high"] > hi:
                nb["high"] = hi
        out.append(nb)
    return out


def _atr_wilder(rows, period=14):
    """Wilder ATR。rows: [{'high','low','close'}, ...]，返回等长 list（前 period 个为 None）。"""
    n = len(rows)
    atr = [None] * n
    if n < period + 1:
        return atr
    tr = [None] * n
    for i in range(1, n):
        h = rows[i]["high"]; l = rows[i]["low"]; pc = rows[i - 1]["close"]
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
    first = [tr[i] for i in range(1, period + 1) if tr[i] is not None]
    if len(first) < period:
        return atr
    a = sum(first) / float(len(first))
    atr[period] = a
    for i in range(period + 1, n):
        if tr[i] is None:
            continue
        a = (a * (period - 1) + tr[i]) / float(period)
        atr[i] = a
    return atr


# ============================ 1 分钟历史缓存 ============================

def _cache_path(code):
    return os.path.join(_CACHE_DIR, "%s_1min.pkl" % code.replace("/", "_").replace("\\", "_"))


def _load_cache(code):
    p = _cache_path(code)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "rb") as f:
            obj = pickle.load(f)
        if isinstance(obj, dict) and isinstance(obj.get("bars"), list):
            return obj
    except Exception:
        pass
    return {}


def _save_cache(code, obj):
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = _cache_path(code) + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(obj, f)
        os.replace(tmp, _cache_path(code))
        return True
    except Exception:
        return False


def _fetch_bars_page(code, start, count, category="1min"):
    """分页拉取历史 K 线（start = 自最新往回的偏移）。返回 core.futures_service 的 bar 列表。"""
    cat = fut._FUT_BARS_CATEGORY.get(category, fut._FUT_BARS_CATEGORY["1min"])

    def fn(api):
        bars = api.get_instrument_bars(cat, fut.FUTURES_MARKET, code, start, count)
        out = []
        for d in (bars or []):
            try:
                out.append({
                    "date": str(d.get("datetime") or ""),
                    "open": float(d["open"]), "high": float(d["high"]),
                    "low": float(d["low"]), "close": float(d["close"]),
                    "vol": int(d.get("trade") or 0),
                })
            except Exception:
                continue
        return out

    return fut._safe_query(fn) or []


_FRESH_KEEP_MIN = 3   # 增量合并时只允许「最新 N 分钟」内的 bar 被新页覆盖（正在形成的那根）


def _merge_bars(old, new, fresh_keep=_FRESH_KEEP_MIN):
    """按 date 合并去重，升序排列。**已收盘的历史 bar 一经写入即冻结。**

    为什么必须冻结（2026-09-21 实测事故，症状：下午腿信号在「13:41」与「13:02」间来回跳）：
    扩展行情池每次随机选一台券商站取「最新一页」（700 根 ≈ 3 天），不同站对同一根
    **已收盘**分钟 bar 的高/低/量存在微差。早期实现是无条件覆盖
    （``idx[b["date"]] = b``），于是上午已收盘的 bar 每 3 秒被改写一次
    （实测：2026-09-18 / 09-21 的 09:31-11:30 共 120 根，根数不变、数值变了）。
    5 分钟聚合的高低 -> Wilder ATR 递归链 -> dev_atr -> dev_z 全跟着漂移，
    z(11:30) 在 -0.729 / -0.753 之间摇摆，而 k_short=-0.75 恰好夹在中间 —— 阈值
    穿越判定随之翻转，同一个上午算出来的信号在下午反复出现/消失。

    规则：以「缓存与新页中最新的那根 bar」为基准，只有最近 ``fresh_keep`` 分钟内的 bar
    允许被新页覆盖（正在形成的那根必须能刷新）；更早的一律保留缓存值（首次写入即冻结）。
    需要彻底重建历史时删掉 data/vwap_cache/<code>_1min.pkl 后重拉即可。
    """
    if not new:
        return list(old)
    if not old:
        out = list(new)
        out.sort(key=lambda x: x["date"])
        return out

    def k16(b):
        return str(b.get("date") or "")[:16]

    newest = max([k16(b) for b in old] + [k16(b) for b in new])
    try:
        cutoff = (datetime.strptime(newest, "%Y-%m-%d %H:%M")
                  - timedelta(minutes=int(fresh_keep))).strftime("%Y-%m-%d %H:%M")
    except Exception:
        cutoff = newest   # 时间格式异常时最保守：只允许覆盖最新那一根

    idx = {}
    for b in old:
        idx[b["date"]] = b
    for b in new:
        if b["date"] not in idx or k16(b) >= cutoff:
            idx[b["date"]] = b
    out = list(idx.values())
    out.sort(key=lambda x: x["date"])
    return out


def get_history_1min(code, days=30, force=False):
    """取最近 days 个交易日的 1 分钟 K 线（磁盘缓存 + 增量更新）。

    返回 (bars, meta)；bars 为升序 [{date,open,high,low,close,vol}, ...]。
    meta = {'cached':bool, 'days':int, 'n':int, 'built_ts':float}
    """
    need = int(days) * BARS_PER_DAY
    cache = {} if force else _load_cache(code)
    bars = cache.get("bars") or []
    built_ts = cache.get("ts") or 0

    pulled = False
    # 1) 增量：每次只拉最新一页（覆盖当日新增），与缓存合并。
    #    ts 只在真正拉到数据时才刷新——之前无条件刷新 ts 导致增量条件永不成立，
    #    当日 1 分钟 bar 永远停留在首次构建缓存时的位置。
    #    注意：_merge_bars 会**冻结已收盘的历史 bar**（只有最新 3 分钟允许被覆盖）。
    #    若历史 bar 被不同券商站的微差数据改写，ATR/VWAP/dev_z 会漂移，已发生的信号
    #    就会在下午反复出现/消失（2026-09-21 实测事故，详见 _merge_bars docstring）。
    if bars and (time.time() - built_ts) > 3:
        head = _fetch_bars_page(code, 0, _PAGE)
        if head:
            bars = _merge_bars(bars, head)
            pulled = True

    # 2) 不足则分页补历史（服务端每页上限 700 根）
    if len(bars) < need:
        pages = []
        start = len(bars)
        # 已有数据时从 len(bars) 处继续往回翻；空缓存从第 0 页开始
        guard = 0
        while len(bars) + sum(len(p) for p in pages) < need and guard < 40:
            guard += 1
            page = _fetch_bars_page(code, start, _PAGE)
            if not page:
                break
            # 与已有区间完全重叠（服务端到底）则停止
            have = set(b["date"] for b in bars)
            fresh = [b for b in page if b["date"] not in have]
            if not fresh:
                break
            pages.append(fresh)
            start += _PAGE
        if pages:
            merged = list(bars)
            for p in pages:
                merged = _merge_bars(merged, p)
            bars = merged
            pulled = True

    # 未拉到新数据时保留原 ts，下次调用仍会触发增量
    meta = {"ts": time.time() if pulled else built_ts, "days": days, "n": len(bars)}
    if len(bars) >= min(need, _PAGE) or not cache:
        _save_cache(code, {"bars": bars, "ts": meta["ts"]})
    return bars, meta


# ============================ 基线（mu / sd by pos） ============================

def _day_dev_atr(day_bars, atr_by_time):
    """计算某交易日每根 1 分钟的 dev_atr。返回长度 240 的 list（缺失为 None）。

    atr_by_time: {当日 'HH:MM' -> ATR(5min)}，只取该时点已闭合的 5 分钟 bar（无前视）。
    """
    out = [None] * BARS_PER_DAY
    cum_pv = 0.0
    cum_v = 0.0
    atr_prev = None
    for _, b in enumerate(day_bars):
        _, hm = _hms(b["date"])
        pos = _pos_of(hm)
        if pos is None or pos >= BARS_PER_DAY:
            continue
        v = b["vol"] or 0
        if v > 0:
            # 权重价用 close：实测精确复现通达信分时均价线（见模块文档修正 1）
            cum_pv += b["close"] * v
            cum_v += v
        # ATR：取该时点已闭合的最后一个 5 分钟 bar 的值
        a = atr_by_time.get(hm)
        if a is not None:
            atr_prev = a
        if cum_v > 0 and atr_prev and atr_prev > 0:
            vwap = cum_pv / cum_v
            dev = b["close"] - vwap
            out[pos] = dev / atr_prev
    return out


def _build_baseline(hist, atr5_map, days=30):
    """对最近 days 个交易日，按 pos 统计 dev_atr 的 mu / sd。

    atr5_map: {'YYYY-MM-DD': {'HH:MM': atr, ...}, ...}
    返回 {'mu':[240], 'sd':[240], 'days':int}
    """
    # 按日期分组
    by_day = {}
    for b in hist:
        d, hm = _hms(b["date"])
        if _pos_of(hm) is None:
            continue
        by_day.setdefault(d, []).append(b)
    days_list = sorted(by_day.keys())[-days:]

    samples = [[] for _ in range(BARS_PER_DAY)]
    for d in days_list:
        row = _day_dev_atr(by_day[d], atr5_map.get(d) or {})
        for pos in range(BARS_PER_DAY):
            v = row[pos]
            if v is not None:
                samples[pos].append(v)

    mu = [None] * BARS_PER_DAY
    sd = [None] * BARS_PER_DAY
    for pos in range(BARS_PER_DAY):
        xs = samples[pos]
        if len(xs) >= 5:
            m = sum(xs) / float(len(xs))
            var = sum((x - m) ** 2 for x in xs) / float(len(xs) - 1) if len(xs) > 1 else 0.0
            mu[pos] = m
            sd[pos] = (var ** 0.5) if var > 1e-12 else 0.0
    return {"mu": mu, "sd": sd, "days": len(days_list)}


def _agg_5min(hist):
    """把 1 分钟序列按时钟 5 分钟边界聚合，等价于 pandas
    ``resample('5min', closed='right', label='right')``。

    报告 §3.2 已用 12768 根逐根比对验证：由此得到的 5 分钟与通达信原生
    ``fzline/47#*.lc5`` 完全一致。区间口径 (09:30, 09:35] -> 09:35。
    返回 [(date, 'HH:MM', {o,h,l,c,v}), ...] 升序。
    """
    groups = {}
    order = []
    for b in hist:
        d, hm = _hms(b["date"])
        if len(hm) < 5:
            continue
        try:
            h = int(hm[:2]); m = int(hm[3:5])
        except Exception:
            continue
        # 右端点：ceil(m / 5) * 5；满 60 则进位到下一小时
        mm = ((m + 4) // 5) * 5
        hh = h
        if mm >= 60:
            mm = 0
            hh = h + 1
        key = (d, "%02d:%02d" % (hh, mm))
        g = groups.get(key)
        if g is None:
            g = {"date": d, "time": "%02d:%02d" % (hh, mm),
                 "open": b["open"], "high": b["high"], "low": b["low"],
                 "close": b["close"], "vol": b["vol"]}
            groups[key] = g
            order.append(key)
        else:
            g["high"] = max(g["high"], b["high"])
            g["low"] = min(g["low"], b["low"])
            g["close"] = b["close"]
            g["vol"] += b["vol"]
    order.sort()
    return [groups[k] for k in order]


def _atr5_map(hist, period=14):
    """由 1 分钟历史聚合出 5 分钟并算 Wilder ATR，映射为 {'YYYY-MM-DD': {'HH:MM': atr}}。

    全局连续计算（跨日不重置），与回测引擎口径一致。
    计算前先经 _sanitize_for_atr 剔除开盘集合竞价跳空。
    """
    bars5 = _agg_5min(_sanitize_for_atr(hist))
    out = {}
    if not bars5:
        return out
    atr = _atr_wilder(bars5, period)
    for i, b in enumerate(bars5):
        if atr[i] is None:
            continue
        out.setdefault(b["date"], {})[b["time"]] = atr[i]
    return out


# ============================ 当日序列 + 信号模拟 ============================

def _simulate_day(day_bars, feats, params, leg='pm'):
    """按报告规则在当日 1 分钟序列上模拟开平仓。

    leg='pm'（下午动量）: dev_z 向上突破 +k_long 做多 / 向下跌破 k_short 做空。
    leg='am'（上午开盘驱动）: ret1545 = 9:45 收 ÷ 9:30 开 − 1，≥ +k_pct 做多 / ≤ −k_pct 做空，
        09:46 开盘入场、上午 11:30 前了结。

    feats: [{'t','pos','close','open','high','low','vwap','dev','dev_atr','dev_z','vol'}, ...]
    params: DEFAULT_PARAMS / AM_DEFAULT_PARAMS 兼容 dict
    返回 (signals, last_state)
    """
    k_long = float(params.get("k_long", 1.0))
    k_short = float(params.get("k_short", -1.5))
    k_pct = float(params.get("k_pct", 0.40))   # 上午腿：ret1545 门槛（%）
    rr = float(params.get("rr", 1.5))
    atr_mult = float(params.get("atr_mult", 1.5))
    time_stop = int(params.get("time_stop", 60))
    s_start = _minute_of(params.get("session_start", "13:00" if leg != 'am' else "09:46"))
    s_end = _minute_of(params.get("session_end", "14:55" if leg != 'am' else "11:30"))

    # 上午腿：开盘 15 分钟的方向 ret1545 = 9:45 收 ÷ 9:30 开 − 1
    # （open[0]=09:31 那根的开盘价即 9:30 开盘价；close[14]=09:45 收盘）
    ret1545 = None
    if leg == 'am':
        o0 = c14 = None
        for f in feats:
            if f.get("pos") == 0:
                o0 = f.get("open")
            elif f.get("pos") == 14:
                c14 = f.get("close")
        if o0 and o0 != 0 and c14 is not None:
            ret1545 = (c14 / o0 - 1.0) * 100.0   # 百分号口径，与 k_pct 一致（build_vwap_view 显示值同口径）

    signals = []
    state = 0            # 0 flat / 1 long / -1 short
    entry = stop = target = None
    entry_i = -1
    pending = None       # 待下一根开盘成交的方向
    warmup = int(params.get("warmup", WARMUP_BARS))
    last_z = None        # 上一根有效 dev_z（突破判定基准）；跨午休有意不清空，见下方下午腿注释

    def close(i, price, why):
        nonlocal state, entry, stop, target, entry_i
        if state == 0 or entry is None:
            return
        risk = abs(entry - stop) if stop else 0.0
        pnl_pts = (price - entry) * state
        pnl_r = (pnl_pts / risk) if risk > 1e-9 else 0.0
        signals[-1].update({
            "exit_t": feats[i]["t"], "exit_price": round(price, 2),
            "exit_why": why, "pnl_pts": round(pnl_pts, 2),
            "pnl_r": round(pnl_r, 3), "hold": i - entry_i,
        })
        state = 0; entry = stop = target = None; entry_i = -1

    for i, f in enumerate(feats):
        t = _minute_of(f["t"])
        z = f["dev_z"]
        prev_z = last_z    # 突破判定的「前一根」基准（None = 尚无有效值）

        # 1) 上一根确认的信号，在本根开盘成交
        if pending is not None and state == 0:
            atr_now = f.get("atr") or 0.0
            ep = f["open"]
            if pending > 0:
                stop = ep - atr_mult * atr_now
                target = ep + rr * atr_mult * atr_now
                state = 1
            else:
                stop = ep + atr_mult * atr_now
                target = ep - rr * atr_mult * atr_now
                state = -1
            entry = ep; entry_i = i
            signals.append({
                "t": f["t"], "side": "long" if pending > 0 else "short",
                "price": round(ep, 2), "stop": round(stop, 2),
                "target": round(target, 2), "atr": round(atr_now, 2),
                "dev_z": None if f["dev_z"] is None else round(f["dev_z"], 3),
                "exit_t": None, "exit_price": None, "exit_why": "hold",
                "pnl_pts": None, "pnl_r": None, "hold": 0,
            })
            pending = None

        # 2) 持仓中：止损 / 止盈 / 时间止损 / 尾盘强平
        if state != 0:
            if state == 1:
                if f["low"] <= stop:
                    close(i, stop, "stop")
                elif f["high"] >= target:
                    close(i, target, "target")
            else:
                if f["high"] >= stop:
                    close(i, stop, "stop")
                elif f["low"] <= target:
                    close(i, target, "target")
            if state != 0 and (i - entry_i) >= time_stop:
                close(i, f["close"], "time")
            elif state != 0 and t >= s_end:
                close(i, f["close"], "eod")

        # 3) 本根收盘确认信号 -> 下一根开盘成交
        if leg == 'am':
            # 上午腿：仅 9:45（pos 14）那一根收盘后确认方向（ret1545 单点触发），
            # 下一根（09:46）开盘成交，与「开盘 15 分钟定方向、09:46 跟进」完全一致。
            if state == 0 and pending is None and f.get("pos") == 14 and ret1545 is not None:
                if ret1545 >= k_pct:
                    pending = 1
                elif ret1545 <= -k_pct:
                    pending = -1
        else:
            # 下午腿：突破阈值口径。仅当上一根有效 dev_z 在阈值内侧、本根穿到外侧时触发，
            # 而非每根都超过阈值即触发（避免持续超阈期间反复挂单）。
            #
            # 【午休边界口径 · 2026-09-21 用户拍板，勿改】
            #   prev_z 取「上一根有效 bar」的 dev_z，且**跨午休不清空**：
            #   下午首根是 13:01（通达信 1 分钟 bar 没有 13:00 这根，见 _pos_of），
            #   它的突破基准 = 上午最后一根（11:30）的 dev_z。
            #   这与报告引擎 vwap_study.build_signal 完全等价——那边用 dz.shift(1)
            #   判穿越，而 .lc1 里午休段没有行，13:01 的上一行恰好就是 11:30。
            #   实证 2026-09-21 ICL9：13:02 空单正是靠 z(11:30)=-0.696 > k_short=-0.75
            #   >= z(13:01)=-0.981 成立；若改成「只在下午时段内判穿越」，该笔会消失。
            #   注意下面 s_start <= t < s_end 是必须的：它保证 11:30 那根（以及上午任何
            #   一根）只能充当基准、不能开出下午腿仓位——去掉它 11:30 就会自己触发。
            #   回归测试：skills/xtquant-vwap-board-legs/assets/vwap_lunch_baseline_test.py
            if state == 0 and pending is None and i >= warmup and s_start <= t < s_end:
                if z is not None and prev_z is not None:
                    if prev_z < k_long <= z:                  # 向上突破做多阈值
                        pending = 1
                    elif prev_z >= k_short and z < k_short:   # 向下突破做空阈值（逐字对齐报告 build_signal）
                        pending = -1

        # 更新 last_z（仅有效值参与突破判定）；跨午休有意不清空，见上方下午腿注释
        if z is not None:
            last_z = z

    last_state = {
        "state": "long" if state > 0 else ("short" if state < 0 else "flat"),
        "entry": None if entry is None else round(entry, 2),
        "stop": None if stop is None else round(stop, 2),
        "target": None if target is None else round(target, 2),
        "hold": (len(feats) - 1 - entry_i) if state != 0 else 0,
    }
    return signals, last_state


def _coerce_num(d, key, val):
    """把 val 转 float 写入 d[key]；失败静默跳过（保留默认值）。"""
    try:
        d[key] = float(val)
    except Exception:
        pass


def build_vwap_view(code, params=None, days=30, leg='pm'):
    """构建 VWAP 模式的完整视图数据（供前端绘图）。

    leg='pm'（下午动量）：dev_z 偏离度策略，需历史基线（mu/sd by pos）+ ATR(5min)。
    leg='am'（上午开盘驱动）：ret1545 定方向，止损 ATR(5min)，不依赖 dev_z 基线。
    leg='both'（双腿同屏）：一次响应同时给出两条腿的子序列(r=ret1545% / z=dev_z)、
        两腿信号（分别打 leg='am'/'pm' 标记）与各自末态(last_am/last_pm)。

    传入 params 约定（均为可选覆盖）：
      k_pct / am_k = 上午门槛(%)，am_rr = 上午盈亏比；
      k_long / k_short = 下午阈值，pm_rr = 下午盈亏比；
      兼容单腿旧键 'rr'（am 视作 am_rr、pm 视作 pm_rr）；atr_mult/atr_period/time_stop 双腿共用。

    返回 dict：
      ok/code/date/leg/params/baseline/atr5/ret1545/series/signals/last/last_am/last_pm/meta
    """
    mode = str(leg or 'pm').lower()
    if mode not in ('am', 'pm', 'both'):
        mode = 'pm'

    # 两套参数：上午腿(AM) / 下午腿(PM)，分别合并调用方覆盖值
    p_am = dict(AM_DEFAULT_PARAMS)
    p_pm = dict(DEFAULT_PARAMS)
    if params:
        if "k_pct" in params:
            _coerce_num(p_am, "k_pct", params["k_pct"])
        if "am_k" in params:              # URL 别名（上午门槛）
            _coerce_num(p_am, "k_pct", params["am_k"])
        if "am_rr" in params:
            _coerce_num(p_am, "rr", params["am_rr"])
        if "k_long" in params:
            _coerce_num(p_pm, "k_long", params["k_long"])
        if "k_short" in params:
            _coerce_num(p_pm, "k_short", params["k_short"])
        if "pm_rr" in params:
            _coerce_num(p_pm, "rr", params["pm_rr"])
        # 兼容单腿旧键 'rr'：仅在未显式给 am_rr/pm_rr 时兜底
        if "rr" in params:
            if mode == 'am':
                _coerce_num(p_am, "rr", params["rr"])
            elif mode == 'pm':
                _coerce_num(p_pm, "rr", params["rr"])
            else:
                _coerce_num(p_am, "rr", params["rr"])
                _coerce_num(p_pm, "rr", params["rr"])
        for kk in ("atr_mult", "atr_period", "time_stop"):
            if kk in params:
                _coerce_num(p_am, kk, params[kk])
                _coerce_num(p_pm, kk, params[kk])
    p_am["warmup"] = WARMUP_BARS
    p_pm["warmup"] = WARMUP_BARS

    need_am = mode in ('am', 'both')
    need_pm = mode in ('pm', 'both')

    with _LOCK:
        hist, meta = get_history_1min(code, days=int(days))
        # 两腿止损统一 5 分钟口径（报告 §②③ 共用同一止损基准 ≈8.5→12.76）
        atr_map = _atr5_map(hist, period=int(p_pm["atr_period"])) if hist else {}
        if need_pm:
            # dev_z 基线只有下午腿需要；上午腿不依赖，省一次基线计算
            base = _build_baseline(hist, atr_map, days=int(days))
        else:
            base = {"mu": [None] * BARS_PER_DAY, "sd": [None] * BARS_PER_DAY, "days": 0}

    if not hist:
        return {"ok": False, "code": code, "error": "no history bars", "series": [], "signals": []}

    # 当日 = 历史中最后一个交易日
    by_day = {}
    for b in hist:
        d, hm = _hms(b["date"])
        if _pos_of(hm) is None:
            continue
        by_day.setdefault(d, []).append(b)
    if not by_day:
        return {"ok": False, "code": code, "error": "no valid session bars", "series": [], "signals": []}
    day = sorted(by_day.keys())[-1]
    day_bars = sorted(by_day[day], key=lambda x: x["date"])

    # 当日每根 1 分钟的特征
    feats = []
    cum_pv = 0.0
    cum_v = 0.0
    atr_prev = None
    open0 = None          # 9:30 开盘价（pos 0 的 open），上午腿算 ret1545 / ret_open 用
    mu, sd = base["mu"], base["sd"]
    for b in day_bars:
        _, hm = _hms(b["date"])
        pos = _pos_of(hm)
        if pos is None:
            continue
        v = b["vol"] or 0
        if v > 0:
            # 权重价用 close：实测精确复现通达信分时均价线（见模块文档修正 1）
            cum_pv += b["close"] * v
            cum_v += v
        if pos == 0:
            open0 = b["open"]
        a = (atr_map.get(day) or {}).get(hm)
        if a is not None:
            atr_prev = a
        vwap = (cum_pv / cum_v) if cum_v > 0 else None
        dev = (b["close"] - vwap) if vwap else None
        dev_atr = (dev / atr_prev) if (dev is not None and atr_prev and atr_prev > 0) else None
        m = mu[pos] if pos < BARS_PER_DAY else None
        s = sd[pos] if pos < BARS_PER_DAY else None
        dev_z = None
        if dev_atr is not None and m is not None and s is not None and s > 1e-9:
            dev_z = (dev_atr - m) / s
        # 开盘以来收益率 ret_open = close / 9:30开 − 1（%），9:45 处即 ret1545。
        # 恒算：双腿同屏(leg='both')时作为上午腿子序列；单腿时也算（多算一个字段，无副作用）。
        r_pct = None
        if open0 and open0 != 0:
            r_pct = (b["close"] / open0 - 1.0) * 100.0
        feats.append({
            "t": hm, "pos": pos,
            "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"],
            "vol": v,
            "vwap": vwap, "dev": dev, "dev_atr": dev_atr, "dev_z": dev_z,
            "atr": atr_prev, "r": r_pct,
        })

    # 上午腿 ret1545（= 9:45 收 ÷ 9:30 开 − 1），供前端标题/信号条展示。恒算。
    ret1545 = None
    if open0 and open0 != 0:
        c14 = None
        for f in feats:
            if f.get("pos") == 14:
                c14 = f.get("close")
        if c14 is not None:
            ret1545 = (c14 / open0 - 1.0) * 100.0

    # 两条腿分别模拟：am 用 p_am（ret1545 单阈值 + 2.0R + 上午窗口 09:46–11:30），
    # pm 用 p_pm（dev_z 双阈值 + 1.5R + 下午窗口 13:00–14:55）。
    # both 时合并信号并给每条打上 leg 标记（前端据此分色/分窗绘制）。
    signals = []
    last_am = None
    last_pm = None
    if need_am:
        am_sig, last_am = _simulate_day(day_bars, feats, p_am, leg='am')
        if mode == 'both':
            for s in am_sig:
                s['leg'] = 'am'
        signals.extend(am_sig)
    if need_pm:
        pm_sig, last_pm = _simulate_day(day_bars, feats, p_pm, leg='pm')
        if mode == 'both':
            for s in pm_sig:
                s['leg'] = 'pm'
        signals.extend(pm_sig)

    # 精简当日序列（只保留前端绘图所需字段，控制响应体积）
    series = [{
        "t": f["t"], "pos": f["pos"], "p": round(f["close"], 2),
        "v": f["vol"], "vw": None if f["vwap"] is None else round(f["vwap"], 2),
        "d": None if f["dev"] is None else round(f["dev"], 3),
        "da": None if f["dev_atr"] is None else round(f["dev_atr"], 4),
        "z": None if f["dev_z"] is None else round(f["dev_z"], 3),
        "r": None if f["r"] is None else round(f["r"], 3),
        "a": None if f["atr"] is None else round(f["atr"], 2),
    } for f in feats]

    last = series[-1] if series else None
    params_out = {
        "leg": mode,
        "rr": p_pm["rr"], "pm_rr": p_pm["rr"], "am_rr": p_am["rr"],
        "k_pct": p_am["k_pct"],
        "k_long": p_pm["k_long"], "k_short": p_pm["k_short"],
        "atr_mult": p_pm["atr_mult"], "atr_period": int(p_pm["atr_period"]),
        "time_stop": int(p_pm["time_stop"]), "time_stop_am": int(p_am["time_stop"]),
        "session_start": p_pm["session_start"], "session_end": p_pm["session_end"],
        "session_start_am": p_am["session_start"], "session_end_am": p_am["session_end"],
        "warmup": WARMUP_BARS,
    }
    return {
        "ok": True,
        "code": code,
        "date": day,
        "leg": mode,
        "params": params_out,
        "baseline": {"days": base["days"], "ok_days": sum(1 for x in mu if x is not None)},
        "atr5": round(atr_prev, 2) if atr_prev else None,
        "ret1545": None if ret1545 is None else round(ret1545, 3),
        "series": series,
        "signals": signals,
        "last": last_pm if last_pm is not None else last_am,
        "last_am": last_am,
        "last_pm": last_pm,
        "meta": {
            "hist_n": len(hist),
            "day_n": len(series),
            "cached_days": meta.get("days"),
            "ts": time.strftime("%H:%M:%S"),
        },
    }
