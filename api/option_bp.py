# -*- coding: utf-8 -*-
"""期权监控蓝图：
    /option/full                 全指标列表（支持筛选/排序/分页/CSV导出）
    /option/full/<code>          单合约全指标详情
    /option/full/stats           字段覆盖率统计
"""
import io
import os
import json
import time
import threading
import datetime as dt
import pandas as pd
from flask import Blueprint, jsonify, request, send_file
from core import option_service
from core import option_exquote_service as exquote
from config import Config

option_bp = Blueprint("option", __name__)


# =========================================================
# 顶层缓存（stale-while-revalidate）
# =========================================================
_OPT_FULL_CACHE = {"df": None, "ts": 0.0}
_OPT_FULL_CACHE_TTL = 6.0          # 秒；覆盖前端 3s/5s 轮询，配合后台刷新不阻塞
_OPT_FULL_REFRESHING = {"flag": False}
_OPT_FULL_LOCK = threading.Lock()


def _compute_full_df(force=False):
    return option_service.option_full_df(force=force)


def _store_full_df(df):
    _OPT_FULL_CACHE["df"] = df
    _OPT_FULL_CACHE["ts"] = time.time()


def _refresh_full_cache_bg(force=False):
    import logging as _lg
    with _OPT_FULL_LOCK:
        if _OPT_FULL_REFRESHING["flag"]:
            return
        _OPT_FULL_REFRESHING["flag"] = True
    try:
        _store_full_df(_compute_full_df(force=force))
    except Exception as e:
        _lg.getLogger(__name__).warning("[option/full] 后台刷新失败: %s", e)
    finally:
        with _OPT_FULL_LOCK:
            _OPT_FULL_REFRESHING["flag"] = False


def _warm_full_cache():
    """Flask 启动预热：后台线程算好首份全量数据，确保首屏（/option/full 及其看板）无感。

    始终强制刷新一份（force=True），使跨日/重启后行情与指标为最新；合约列表沿用
    option_contracts 的「日内复用 + 每日盘前失效」逻辑，同日内不重复发现。
    """
    import logging as _lg
    _lg.getLogger(__name__).info("[option/full] 启动预热开始…")
    try:
        _store_full_df(_compute_full_df(force=True))
        _lg.getLogger(__name__).info(
            "[option/full] 启动预热完成: %d 行", len(_OPT_FULL_CACHE.get("df") or [])
        )
    except Exception as e:
        _lg.getLogger(__name__).warning("[option/full] 启动预热失败: %s", e)


def _get_full_df():
    """全量 DataFrame（OPTION_RT_TTL 秒级缓存）。
    顶层 6s 缓存 + stale-while-revalidate：命中直接返回；过期立即返回旧数据并在
    后台异步刷新（新浪 CON_OP_ 拉取 + Black-Scholes 指标约 15~25s），前端轮询永不阻塞。
    force=1 同步刷新并返回最新。
    """
    try:
        _force = str(request.args.get("force", "")).strip().lower() in ("1", "true", "yes", "y", "是")
    except Exception:
        _force = False
    if _force:
        df = _compute_full_df(force=True)
        _store_full_df(df)
        return df.copy(deep=True)
    _c = _OPT_FULL_CACHE.get("df")
    _ts = _OPT_FULL_CACHE.get("ts") or 0
    _age = time.time() - _ts
    if _c is not None and _age < _OPT_FULL_CACHE_TTL:
        return _c.copy(deep=True)
    if _c is not None:
        threading.Thread(target=_refresh_full_cache_bg, args=(False,), daemon=True).start()
        return _c.copy(deep=True)
    df = _compute_full_df()
    _store_full_df(df)
    return df.copy(deep=True)


# =========================================================
# 筛选 / 排序 / 分页 / 序列化
# =========================================================
def _parse_bool(s):
    if s is None or s == "":
        return None
    return str(s).lower() in ("1", "true", "yes", "y", "是")


def _num_ge(df, col, v):
    return df[pd.to_numeric(df[col], errors="coerce") >= float(v)]


def _num_le(df, col, v):
    return df[pd.to_numeric(df[col], errors="coerce") <= float(v)]


def _apply_filters(df):
    """query string 筛选：
        underlying_like=510050 / 50ETF / 上证50     标的代码或名称模糊
        underlying_in=510500,159922                 标的代码精确多选（合并项）
        exchange=SSE|SZSE                         交易所
        type=C|P                                  认购/认沽
        code_in=10012127,90007051                代码精确(逗号分隔)
        strike_ge=2.5  strike_le=3.0             行权价区间
        days_le=30                               到期剩余天数 ≤
        month=2026-09                           合约到期月份（expiry 前 7 位 YYYY-MM）
        last_ge=0.01  last_le=1.0                期权现价区间
        delta_ge=0.5  delta_le=1.0               Delta 区间
        iv_ge=0.1     iv_le=0.5                  隐含波动率区间
        premium_le=10                             溢价率% ≤
        leverage_ge=5                            杠杆比率 ≥
        eff_leverage_ge=5                        实际杠杆 ≥
        oi_ge=1000                              持仓量(张) ≥
        itm=1                                    只看实值（intrinsic>0）
        otm=1                                    只看虚值（intrinsic<=0）
        traded=1                                 只看有行情（last>0）
    """
    for k, raw in request.args.items():
        v = raw.strip()
        if not v:
            continue
        try:
            if k == "underlying_like":
                pat = v.lower()
                df = df[df["underlying_code"].astype(str).str.lower().str.contains(pat, na=False) |
                        df["underlying_name"].astype(str).str.lower().str.contains(pat, na=False)]
            elif k == "underlying_in":
                codes = {x.strip() for x in v.split(",") if x.strip()}
                if codes:
                    df = df[df["underlying_code"].astype(str).isin(codes)]
            elif k == "exchange":
                df = df[df["exchange"].astype(str).str.upper() == v.upper()]
            elif k == "type":
                df = df[df["type"].astype(str).str.upper() == v.upper()]
            elif k == "code_in":
                codes = {x.strip() for x in v.split(",") if x.strip()}
                df = df[df["code"].astype(str).isin(codes) | df["contract_code"].astype(str).isin(codes)]
            elif k == "strike_ge":  df = _num_ge(df, "strike", v)
            elif k == "strike_le":  df = _num_le(df, "strike", v)
            elif k == "days_le":    df = _num_le(df, "days", v)
            elif k == "month":      df = df[df["expiry"].astype(str).str.slice(0, 7) == v]
            elif k == "last_ge":    df = _num_ge(df, "last", v)
            elif k == "last_le":    df = _num_le(df, "last", v)
            elif k == "delta_ge":   df = _num_ge(df, "delta", v)
            elif k == "delta_le":    df = _num_le(df, "delta", v)
            elif k == "iv_ge":      df = _num_ge(df, "iv", v)
            elif k == "iv_le":      df = _num_le(df, "iv", v)
            elif k == "premium_le": df = _num_le(df, "premium_rate", v)
            elif k == "leverage_ge":df = _num_ge(df, "leverage_ratio", v)
            elif k == "eff_leverage_ge": df = _num_ge(df, "effective_leverage", v)
            elif k == "oi_ge":      df = _num_ge(df, "oi", v)
            elif k == "itm" and _parse_bool(v):
                df = df[pd.to_numeric(df["intrinsic"], errors="coerce") > 0]
            elif k == "otm" and _parse_bool(v):
                df = df[pd.to_numeric(df["intrinsic"], errors="coerce") <= 0]
            elif k == "traded" and _parse_bool(v):
                df = df[pd.to_numeric(df["last"], errors="coerce") > 0]
        except Exception:
            continue
    return df


def _apply_sort(df):
    col = request.args.get("sort")
    asc = _parse_bool(request.args.get("asc", "1")) is not False
    if col and col in df.columns:
        df = df.sort_values(col, ascending=asc, kind="mergesort", na_position="last")
    return df


def _apply_paging(df):
    try:
        limit = int(request.args.get("limit", "0"))
    except Exception:
        limit = 0
    try:
        offset = int(request.args.get("offset", "0"))
    except Exception:
        offset = 0
    total = len(df)
    if offset > 0:
        df = df.iloc[offset:]
    if limit > 0:
        df = df.iloc[:limit]
    return df, total, offset, limit


def _to_records(df):
    df = df.where(pd.notnull(df), "")
    return df.to_dict(orient="records")


_STD_COLUMNS = ["code", "exchange", "underlying_code", "underlying_name", "type",
                "contract_name", "contract_code", "strike", "expiry", "days",
                "last", "preclose", "change_pct", "bid", "ask", "volume", "oi",
                "amount", "activity",
                "high", "low", "amplitude", "underlying_price", "intrinsic",
                "time_value", "moneyness", "premium_rate", "leverage_ratio",
                "iv", "delta", "gamma", "vega", "theta", "effective_leverage"]


@option_bp.route("/full")
def option_full():
    fmt = request.args.get("format", "json").lower()
    df = _get_full_df()
    total_before = len(df)
    df = _apply_filters(df)
    total_filtered = len(df)
    df = _apply_sort(df)
    df, total, offset, limit = _apply_paging(df)

    if fmt == "csv":
        buf = io.BytesIO()
        buf.write(b"\xef\xbb\xbf")
        s = df.to_csv(index=False)
        buf.write(s.encode("utf-8"))
        buf.seek(0)
        fname = f"option_full_{dt.date.today().isoformat()}.csv"
        return send_file(buf, mimetype="text/csv; charset=utf-8", as_attachment=True, download_name=fname)

    std_cols = [c for c in _STD_COLUMNS if c in df.columns]
    return jsonify({
        "ok": True,
        "total": total,
        "total_filtered": total_filtered,
        "total_raw": total_before,
        "offset": offset,
        "limit": limit,
        "count": len(df),
        "columns_count": len(df.columns),
        "columns": list(df.columns),
        "standard_columns": std_cols,
        "derived_columns": [c for c in list(df.columns) if c not in std_cols],
        "data": _to_records(df),
    })


@option_bp.route("/full/stats")
def option_full_stats():
    df = _get_full_df()
    stats = {}
    for c in df.columns:
        s = df[c]
        stats[c] = int(((~s.isna()) & (s.astype(str).str.strip() != "")).sum())
    coverage = {c: {"non_null": stats.get(c, 0), "total": len(df),
                    "ratio": round(stats.get(c, 0) / len(df), 4) if len(df) else 0}
                for c in _STD_COLUMNS if c in df.columns}
    return jsonify({
        "ok": True,
        "count": len(df),
        "total_columns": len(df.columns),
        "coverage": coverage,
    })


@option_bp.route("/full/<path:code>")
def option_full_one(code):
    df = _get_full_df()
    tgt = code.strip().upper()
    row = df[df["code"].astype(str).str.upper() == tgt]
    if not len(row):
        row = df[df["contract_code"].astype(str).str.upper() == tgt]
    if not len(row):
        row = df[df["underlying_code"].astype(str).str.contains(tgt, case=False, na=False) |
                df["underlying_name"].astype(str).str.contains(tgt, case=False, na=False)]
    if not len(row):
        return jsonify({"ok": False, "error": f"未找到期权合约 code={code}", "count": 0}), 404
    return jsonify({
        "ok": True,
        "count": len(row),
        "columns_count": len(df.columns),
        "standard_columns": [c for c in _STD_COLUMNS if c in df.columns],
        "data": _to_records(row),
    })


# =========================================================
# 自选股（watchlist）：持久化到磁盘 JSON
# =========================================================
_WATCHLIST_FILE = os.path.join(Config.DATA_DIR, "option_watchlist.json")
_WATCHLIST_LOCK = threading.Lock()
_WATCHLIST_CACHE = None


def _load_watchlist():
    """读取自选股代码集合（进程内缓存 + 磁盘 JSON 兜底）。"""
    global _WATCHLIST_CACHE
    with _WATCHLIST_LOCK:
        if _WATCHLIST_CACHE is not None:
            return _WATCHLIST_CACHE
        try:
            with open(_WATCHLIST_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            codes = set(str(c) for c in data.get("codes", []) if c)
        except FileNotFoundError:
            codes = set()
        except Exception:
            codes = set()
        _WATCHLIST_CACHE = codes
        return codes


def _save_watchlist(codes):
    """写回自选股代码集合（去重 + 排序 + 进程内缓存）。"""
    codes = set(str(c) for c in codes if c)
    global _WATCHLIST_CACHE
    with _WATCHLIST_LOCK:
        _WATCHLIST_CACHE = codes
        try:
            os.makedirs(os.path.dirname(_WATCHLIST_FILE), exist_ok=True)
            with open(_WATCHLIST_FILE, "w", encoding="utf-8") as fh:
                json.dump({"codes": sorted(codes)}, fh, ensure_ascii=False, indent=2)
        except Exception as e:
            import logging as _lg
            _lg.getLogger(__name__).warning("[option/watchlist] 保存失败: %s", e)
    return codes


@option_bp.route("/watchlist", methods=["GET"])
def option_watchlist_get():
    return jsonify({"ok": True, "codes": sorted(_load_watchlist())})


@option_bp.route("/watchlist/add", methods=["POST"])
def option_watchlist_add():
    body = request.get_json(silent=True) or {}
    code = str(body.get("code") or "").strip()
    if not code:
        return jsonify({"ok": False, "error": "code 不能为空"}), 400
    codes = _load_watchlist()
    codes.add(code)
    _save_watchlist(codes)
    return jsonify({"ok": True, "codes": sorted(_load_watchlist())})


@option_bp.route("/watchlist/remove", methods=["POST"])
def option_watchlist_remove():
    body = request.get_json(silent=True) or {}
    code = str(body.get("code") or "").strip()
    if not code:
        return jsonify({"ok": False, "error": "code 不能为空"}), 400
    codes = _load_watchlist()
    codes.discard(code)
    _save_watchlist(codes)
    return jsonify({"ok": True, "codes": sorted(_load_watchlist())})


# =========================================================
# 扩展行情(ExHq) 看盘：分时 / 盘口 / 逐笔
#   数据契约与「可转债看盘板」board-rt 完全一致，便于前端复用同一组件。
# =========================================================
_EXQ_CACHE = {}


def _exq_unavailable():
    return (exquote is None) or (not getattr(exquote, "_EXHQ_OK", False))


@option_bp.route("/exquote/minute/<path:code>")
def option_exquote_minute(code):
    """期权当日分时（扩展行情 get_minute_time_data）。"""
    try:
        if _exq_unavailable():
            return jsonify({"ok": False, "src": "exhq", "data": [], "error": "exhq unavailable"})
        ttl = int(request.args.get("ttl", "10"))
        key = "opt_exq_minute_%s" % code
        cached = _EXQ_CACHE.get(key)
        if cached and (time.time() - cached[1]) < ttl:
            return jsonify({"ok": True, "src": "exhq", "data": cached[0]})
        data = exquote.exquote_minute(code)
        _EXQ_CACHE[key] = (data, time.time())
        return jsonify({"ok": bool(data), "src": "exhq", "data": data})
    except Exception as e:
        return jsonify({"ok": False, "src": "exhq", "data": [], "error": str(e)})


@option_bp.route("/exquote/quote/<path:code>")
def option_exquote_quote(code):
    """期权五档盘口（扩展行情 get_instrument_quote）。"""
    try:
        if _exq_unavailable():
            return jsonify({"ok": False, "src": "exhq", "data": {}, "error": "exhq unavailable"})
        ttl = int(request.args.get("ttl", "3"))
        key = "opt_exq_quote_%s" % code
        cached = _EXQ_CACHE.get(key)
        if cached and (time.time() - cached[1]) < ttl:
            return jsonify({"ok": True, "src": "exhq", "data": cached[0]})
        data = exquote.exquote_quote(code)
        _EXQ_CACHE[key] = (data, time.time())
        return jsonify({"ok": bool(data), "src": "exhq", "data": data})
    except Exception as e:
        return jsonify({"ok": False, "src": "exhq", "data": {}, "error": str(e)})


@option_bp.route("/exquote/tick/<path:code>")
def option_exquote_tick(code):
    """期权逐笔成交（扩展行情 get_transaction_data）。"""
    try:
        if _exq_unavailable():
            return jsonify({"ok": False, "src": "exhq", "data": [], "error": "exhq unavailable"})
        count = int(request.args.get("count", "40"))
        ttl = int(request.args.get("ttl", "3"))
        key = "opt_exq_tick_%s" % code
        cached = _EXQ_CACHE.get(key)
        if cached and (time.time() - cached[1]) < ttl:
            return jsonify({"ok": True, "src": "exhq", "data": cached[0]})
        data = exquote.exquote_tick(code, count=count)
        _EXQ_CACHE[key] = (data, time.time())
        return jsonify({"ok": bool(data), "src": "exhq", "data": data})
    except Exception as e:
        return jsonify({"ok": False, "src": "exhq", "data": [], "error": str(e)})
