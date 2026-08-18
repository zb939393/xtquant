# -*- coding: utf-8 -*-
"""可转债蓝图：
    /cb/panorama                 旧版全景
    /cb/top_dual_low?n=10        双低排名
    /cb/full                     全指标列表（支持筛选/排序/分页/CSV导出）
    /cb/full/<code>              单券全指标详情
    /cb/full_stats               非空率统计 + 总字段数
"""
import io
import time
import threading
import datetime as dt
import pandas as pd
from flask import Blueprint, jsonify, request, send_file
from core import ak_service

cb_bp = Blueprint("cb", __name__)


# =========================================================
# 旧接口
# =========================================================
@cb_bp.route("/panorama")
def panorama():
    df = ak_service.cb_panorama()
    return jsonify({
        "ok": True,
        "count": len(df),
        "columns": list(df.columns),
        "data": df.fillna("").to_dict(orient="records"),
    })


@cb_bp.route("/top_dual_low")
def top_dual_low():
    n = int(request.args.get("n", "10"))
    df = ak_service.cb_top_dual_low(n=n)
    return jsonify({
        "ok": True, "count": len(df),
        "data": df.fillna("").to_dict(orient="records"),
    })


# =========================================================
# 全指标工具函数
# =========================================================
def _parse_bool(s):
    if s is None or s == "": return None
    return str(s).lower() in ("1","true","yes","y","是")


_CB_FULL_CACHE = {"df": None, "ts": 0.0}
_CB_FULL_CACHE_TTL = 6.0  # 秒；覆盖前端 3s/5s 轮询；配合 stale-while-revalidate，任何轮询都不再阻塞重算
_CB_FULL_REFRESHING = {"flag": False}
_CB_FULL_LOCK = threading.Lock()

def _compute_full_df(static_ttl):
    return ak_service.cb_full_metrics(static_ttl=static_ttl)

def _store_full_df(df):
    _CB_FULL_CACHE["df"] = df
    _CB_FULL_CACHE["ts"] = time.time()

def _refresh_full_cache_bg(static_ttl):
    """后台刷新顶层缓存：stale-while-revalidate，避免用户轮询被 8~14s 重算阻塞。"""
    import logging as _lg
    with _CB_FULL_LOCK:
        if _CB_FULL_REFRESHING["flag"]:
            return
        _CB_FULL_REFRESHING["flag"] = True
    try:
        _store_full_df(_compute_full_df(static_ttl))
    except Exception as e:
        _lg.getLogger(__name__).warning("[cb/full] 后台刷新失败: %s", e)
    finally:
        with _CB_FULL_LOCK:
            _CB_FULL_REFRESHING["flag"] = False

def _warm_full_cache():
    """Flask 启动后预热：先算好一份，避免首屏 14s 阻塞。仅当缓存为空时执行。"""
    if _CB_FULL_CACHE.get("df") is None:
        try:
            _store_full_df(_compute_full_df(None))
        except Exception:
            pass

def _get_full_df(static_ttl=None):
    """全量 DataFrame（静态字段长TTL缓存，PyTDX实时列秒级缓存）。
    static_ttl=None 走默认 Config.AK_CACHE_TTL（300s）；
    紧急刷新：/cb/full?force=1 绕过静态TTL与顶层缓存，并同步刷新缓存。
    顶层 6s 缓存 + stale-while-revalidate：命中直接返回；过期立即返回旧数据并在后台
    异步刷新（桥拉取+派生合并约 8~14s），前端轮询永不阻塞。
    """
    try:
        from flask import request as _req
        _force = str(_req.args.get("force","")).strip().lower() in ("1","true","yes","y","是")
    except Exception:
        _force = False
    if _force:
        df = _compute_full_df(0)
        _store_full_df(df)
        return df.copy(deep=True)
    _c = _CB_FULL_CACHE.get("df")
    _ts = _CB_FULL_CACHE.get("ts") or 0
    _age = time.time() - _ts
    if _c is not None and _age < _CB_FULL_CACHE_TTL:
        return _c.copy(deep=True)
    # 缓存过期但存在：立即返回旧数据，后台异步刷新
    if _c is not None:
        threading.Thread(target=_refresh_full_cache_bg, args=(static_ttl,), daemon=True).start()
        return _c.copy(deep=True)
    # 冷启动（理论上已被 _warm_full_cache 覆盖）：同步阻塞计算
    df = _compute_full_df(static_ttl)
    _store_full_df(df)
    return df.copy(deep=True)


def _apply_filters(df):
    """根据 query string 筛选：
        bond_name_like=浦发,
        rating_ge=AA,       (字符串字典序比较；AA- 用 rating_ge=AA-%2D 或直接传值)
        rem_le=5,           rem_size_yi <= 5 亿
        rem_ge=1,
        bond_price_le=130,  bond_price_ge=95,
        prem_le=20,         conv_premium_pct <= 20%
        ytm_ge=0,           ytm_before_tax_calc >= 0
        dual_low_le=130,    dual_low_calc <= 130
        mat_days_le=365,    maturity_days_left <= 365
        call_status_eq=公告要强赎
        exclude_callable=1  剔除强赎状态非"—/空/未触发"的（只留安全的）
        exclude_force_redeem=1 同 exclude_callable
        code_in=123456,123457
        stock_name_like=银行
    """
    for k, raw in request.args.items():
        v = raw.strip();
        if not v: continue
        try:
            if k == "bond_name_like":   df = df[df["bond_name"].astype(str).str.contains(v, case=False, na=False)]
            elif k == "stock_name_like":df = df[df["stock_name"].astype(str).str.contains(v, case=False, na=False)]
            elif k == "code_in":
                codes = {x.strip() for x in v.split(",") if x.strip()}
                df = df[df["bond_code"].astype(str).apply(lambda s: s.replace(".SH","").replace(".SZ","") in codes or s in codes)]
            elif k == "rating_ge" and "rating" in df.columns:
                df = df[(df["rating"].astype(str) >= v) & (df["rating"].astype(str) != "") & (df["rating"].notna())]
            elif k == "rem_le":            df = df[pd.to_numeric(df["rem_size_yi"], errors="coerce") <= float(v)]
            elif k == "rem_ge":            df = df[pd.to_numeric(df["rem_size_yi"], errors="coerce") >= float(v)]
            elif k == "bond_price_le":     df = df[pd.to_numeric(df["bond_price"], errors="coerce") <= float(v)]
            elif k == "bond_price_ge":     df = df[pd.to_numeric(df["bond_price"], errors="coerce") >= float(v)]
            elif k == "prem_le":           df = df[pd.to_numeric(df["conv_premium_pct"], errors="coerce") <= float(v)]
            elif k == "prem_ge":           df = df[pd.to_numeric(df["conv_premium_pct"], errors="coerce") >= float(v)]
            elif k == "ytm_ge":            df = df[pd.to_numeric(df["ytm_before_tax_calc"], errors="coerce") >= float(v)]
            elif k == "ytm_le":            df = df[pd.to_numeric(df["ytm_before_tax_calc"], errors="coerce") <= float(v)]
            elif k == "dual_low_le":       df = df[pd.to_numeric(df["dual_low_calc"], errors="coerce") <= float(v)]
            elif k == "dual_low_ge":       df = df[pd.to_numeric(df["dual_low_calc"], errors="coerce") >= float(v)]
            elif k == "mat_days_le":       df = df[pd.to_numeric(df["maturity_days_left"], errors="coerce") <= float(v)]
            elif k == "mat_days_ge":       df = df[pd.to_numeric(df["maturity_days_left"], errors="coerce") >= float(v)]
            elif k == "call_status_eq" and "call_status" in df.columns:
                df = df[df["call_status"].astype(str) == v]
            elif k in ("exclude_callable","exclude_force_redeem") and _parse_bool(v) and "call_status" in df.columns:
                safe = {"","—","-","nan","未触发","未公告","无","none"}
                df = df[df["call_status"].astype(str).map(lambda s: s.strip() in safe)]
        except Exception:
            continue
    return df


def _apply_sort(df):
    """sort=dual_low_calc&asc=1  asc=0 降序"""
    col = request.args.get("sort")
    asc = _parse_bool(request.args.get("asc", "1")) is not False
    if col and col in df.columns:
        df = df.sort_values(col, ascending=asc, kind="mergesort", na_position="last")
    return df


def _apply_paging(df):
    try:    limit = int(request.args.get("limit", "0"))
    except: limit = 0
    try:    offset = int(request.args.get("offset", "0"))
    except: offset = 0
    total = len(df)
    if offset > 0: df = df.iloc[offset:]
    if limit  > 0: df = df.iloc[:limit]
    return df, total, offset, limit


def _to_records(df):
    """把 NaN/NaT 统一变成 "", 方便 JSON"""
    df = df.where(pd.notnull(df), "")
    for c in df.columns:
        try:
            if pd.api.types.is_datetime64_any_dtype(df[c]):
                df[c] = df[c].astype(str).map(lambda s: "" if s.startswith("NaT") else s)
        except Exception: pass
    return df.to_dict(orient="records")


# =========================================================
# 新接口
# =========================================================
@cb_bp.route("/full")
def cb_full():
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
        fname = f"cb_full_{dt.date.today().isoformat()}.csv"
        return send_file(buf, mimetype="text/csv; charset=utf-8", as_attachment=True, download_name=fname)

    # 判定"非中文列"=标准/派生列：只要列名含任意汉字就算非标准列
    def _has_cjk(s):
        return any("\u4e00" <= ch <= "\u9fff" for ch in str(s))
    eng_cols = sorted([c for c in df.columns if not _has_cjk(str(c))])
    # 取合并后挂在 attrs 上的实时来源元信息：
    #   __rt_src_meta__ : 桥/PyTDX 实时来源与覆盖率（qmt.meta 主来源，桥成功时必有）
    #   __qmt_meta__    : QMT 静态增强元信息（augment_codes 返回）
    #   __pytdx_meta__  : PyTDX 兜底来源
    # 注：旧实现只读 __qmt_meta__（恒为 {}），导致 qmt.meta 永远为空；现改读实时来源属性。
    qmt_meta = {}
    try:
        src_df = _get_full_df()
        _rt = src_df.attrs.get("__rt_src_meta__") or {}
        _qmt = src_df.attrs.get("__qmt_meta__") or {}
        _pytdx = src_df.attrs.get("__pytdx_meta__") or {}
        qmt_meta = {}
        qmt_meta.update(_qmt)
        qmt_meta.update(_pytdx)
        qmt_meta.update(_rt)  # 实时来源放最后，确保 _src/覆盖率反映真实链路
    except Exception:
        pass
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
        "standard_columns": list(c for c in list(df.columns) if c in eng_cols),
        "derived_columns": sorted([c for c in list(df.columns) if c.endswith("_calc") or c.endswith("_rank") or c.endswith("_estimate") or c.endswith("_years_left") or c.endswith("_days_left") or c.endswith("_progress_pct") or c.endswith("_trigger_pct") or c in ("conv_value_100","conv_rate_per_100","remaining_ratio","redemption_gain_pct","premium_decomp_conv_pct","premium_decomp_bond_pct")]),
        "qmt": {
            "meta": qmt_meta,
            "filled_counts": src_df.attrs.get("__qmt_filled_counts__", {}),
            "diagnostic_counts": src_df.attrs.get("__qmt_diag_counts__", {}),
        },
        "data": _to_records(df),
    })


@cb_bp.route("/full/stats")
def cb_full_stats():
    df = _get_full_df()
    stats = {c: int(df[c].astype(str).ne("").sum() & pd.notna(df[c]).sum()) for c in df.columns}
    std_eng = [c for c in list(df.columns) if not any("\u4e00" <= ch <= "\u9fff" for ch in str(c))]
    derived = [c for c in list(df.columns) if c.endswith("_calc") or c.endswith("_rank") or c.endswith("_estimate") or c.endswith("_years_left") or c.endswith("_days_left") or c.endswith("_progress_pct") or c.endswith("_trigger_pct") or c in ("conv_value_100","conv_rate_per_100","remaining_ratio","redemption_gain_pct","premium_decomp_conv_pct","premium_decomp_bond_pct")]
    coverage = {}
    for key_col in ("bond_code","bond_name","stock_code","stock_name","stock_price","bond_price","conv_price","conv_premium_pct",
                    "conv_value_100","dual_low_calc","dual_low_rank",
                    "rem_size_yi","issue_size_yi","rating",
                    "maturity_date","maturity_days_left","maturity_years_left",
                    "ytm_before_tax_calc","ytm_after_tax_pct","maturity_price","coupon_pct",
                    "call_status","call_trigger_price","call_trigger_days","call_progress_pct","call_trigger_pct",
                    "put_trigger_price","put_trigger_pct","adjust_trigger_price","adjust_trigger_pct",
                    "amount_yuan","volume_zhang","daily_amplitude_calc","turnover_rate_calc"):
        if key_col in df.columns:
            coverage[key_col] = {"non_null": int(stats[key_col]),
                                 "total": len(df),
                                 "ratio": round(stats[key_col]/len(df),4) if len(df) else 0}
    qmt_meta = df.attrs.get("__qmt_meta__", {})
    qmt_filled = df.attrs.get("__qmt_filled_counts__", {})
    # 诊断列整体覆盖（带 _qmt 后缀的列）：看有多少行 qmt_via != "none"
    qmt_diag_non_empty = {}
    for dc in ("qmt_via", "qmt_n_raw_fields",
               "bond_name_qmt", "rating", "coupon_pct",
               "total_vol_yi_qmt", "list_date_qmt", "maturity_date_qmt"):
        if dc in df.columns:
            s = df[dc]
            neq = int(((~s.isna()) & (s.astype(str).str.strip() != "")).sum())
            qmt_diag_non_empty[dc] = neq
    # per-code qmt hits 摘要（如果 attrs 里塞了的话）
    per_code_hits = qmt_meta.get("field_hits_per_code") or {}
    if per_code_hits:
        vs = list(per_code_hits.values())
        import statistics
        qmt_code_hits_stats = {
            "sample_codes": len(vs),
            "min": min(vs),
            "max": max(vs),
            "mean": round(sum(vs)/len(vs), 2),
            "median": int(statistics.median(vs)) if vs else 0,
        }
    else:
        qmt_code_hits_stats = {}
    return jsonify({
        "ok": True,
        "count": len(df),
        "total_columns": len(df.columns),
        "standard_columns_count": len(std_eng),
        "derived_columns_count": len(derived),
        "coverage": coverage,
        "qmt": {
            "meta": qmt_meta,
            # 核心列统计 —— 每列拆分 { ak_non_empty, qmt_filled_ak_blank }
            "filled_counts": qmt_filled,
            # 诊断列非空计数（df attr 里的精确统计）
            "diagnostic_counts": df.attrs.get("__qmt_diag_counts__", {}),
            # 兼容旧字段
            "diag_non_empty": qmt_diag_non_empty,
            "code_field_hits_stats": qmt_code_hits_stats,
        },
    })


@cb_bp.route("/full/<path:code>")
def cb_full_one(code):
    df = _get_full_df()
    tgt = code.strip().upper()
    if tgt.endswith((".SH", ".SZ", ".BJ")):
        row = df[df["bond_code"].astype(str).str.upper() == tgt]
    else:
        row = df[df["bond_code"].astype(str).map(lambda s: s.replace(".SH","").replace(".SZ","")) == tgt]
        if not len(row):
            row = df[df["bond_name"].astype(str).str.contains(tgt, na=False, case=False)]
    if not len(row):
        return jsonify({"ok": False, "error": f"未找到转债 code={code}", "count": 0}), 404
    data = _to_records(row)[0]
    eng_keys = [c for c in list(row.columns) if not any("\u4e00" <= ch <= "\u9fff" for ch in str(c))]
    return jsonify({
        "ok": True,
        "columns_count": len(row.columns),
        "standard_columns": eng_keys,
        "data": data,
    })



@cb_bp.route("/full/qmt_probe")
def cb_qmt_probe():
    """检查桥58080 / RPC58600 两条通道的可用性，以及当前 akshare 版本对关键字段的覆盖率。"""
    try:
        from core import qmt_detail_service as qsvc
    except Exception as e:
        return jsonify({"ok": False, "error": "qmt_detail_service import fail: %s" % e}), 500
    channels = qsvc.probe()
    # 拉一次 full 看"实际填了多少"
    try:
        df = _get_full_df()
        filled_counts = df.attrs.get("__qmt_filled_counts__", {})
        meta = df.attrs.get("__qmt_meta__", {})
    except Exception as e:
        filled_counts = {}
        meta = {"via": "none", "reason": "cb_full_metrics fail: %s" % e}
    return jsonify({
        "ok": True,
        "channels": channels,
        "qmt_meta": meta,
        "qmt_filled_counts": filled_counts,
    })
