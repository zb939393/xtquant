# -*- coding: utf-8 -*-
"""股指期货监控蓝图：
    /futures/exquote/minute/<code>  当日分时（扩展行情 get_minute_time_data）
    /futures/exquote/quote/<code>   五档盘口 + 持仓/成交量（扩展行情 get_instrument_quote）
    /futures/exquote/tick/<code>    逐笔成交（扩展行情 get_transaction_data）
    /futures/snapshot               4 个合约实时快照（价/涨跌/昨收/持仓/成交量）
    /futures/popup                 综合看盘·独立弹窗页面（PyWebView 加载）
    /futures/popup/launch          启动 PyWebView 独立窗口（POST）
"""
import os
import subprocess
import sys
import time
import urllib.request
from flask import Blueprint, jsonify, render_template, request, Response
from core import futures_service as fut
from core import ak_service
try:
    from core import vwap_service as vws
except Exception:  # 计算模块不可用时看板 VWAP 模式优雅降级
    vws = None
try:
    from core import vwap_params_store as vstore
except Exception:  # 参数存储不可用时退化为「仅用 URL 参数」
    vstore = None

from config import Config
from .popup_helper import popup_launch
from .popup_config import get_popup_size, POPUP_SIZES

futures_bp = Blueprint("futures", __name__)

# 轻量内存缓存（与期权监控一致：分时 30s、盘口/逐笔 3s）
_EXQ_CACHE = {}


def _exq_unavailable():
    """扩展行情数据源是否不可用。

    判据以 core/futures_service.ext_available()（= _TDX_EXT_SERVERS 池 + TdxExHq_API）为准，
    不再依赖旧 pip 包 tdx_exhq 的 _EXHQ_OK —— 服务器源已切换到长城 7721 池，旧包未安装
    也不影响取数，用它做门禁会导致期指接口被误判为不可用。
    """
    if fut is None:
        return True
    checker = getattr(fut, "ext_available", None)
    if callable(checker):
        try:
            return not checker()
        except Exception:
            return True
    # 兼容极老版本（无 ext_available）：退回旧的 tdx_exhq 标记
    return not getattr(fut, "_EXHQ_OK", False)


# ---- VWAP 策略参数：URL 参数优先，缺失则回退服务端保存的 per-code 参数 ----
# 这样独立弹窗 / iframe 即使不传参、也不与主页面共享 localStorage，仍能拿到同一套阈值。
_VWAP_FIELDS = ("k_long", "k_short", "rr", "days")


def _vwap_eff_params(code, args):
    """合并 URL 参数与服务端持久化的参数，返回 dict（含 days，默认 30）。"""
    saved = vstore.get(code) if vstore else {}
    out = {}
    for k in _VWAP_FIELDS:
        v = args.get(k)
        if v is not None and v != "":
            try:
                out[k] = float(v)
                continue
            except Exception:
                pass
        if k in saved:
            out[k] = saved[k]
    out.setdefault("days", 30.0)
    try:
        out["days"] = int(float(out["days"]))
    except Exception:
        out["days"] = 30
    if out["days"] < 1:
        out["days"] = 30
    return out


def _vwap_cache_key(prefix, code, p):
    tail = "_".join("%s%s" % (k, p[k]) for k in _VWAP_FIELDS if k in p)
    return "%s%s_%s" % (prefix, code, tail)


@futures_bp.route("/exquote/minute/<path:code>")
def futures_exquote_minute(code):
    """期指当日分时（扩展行情 get_minute_time_data），并附带 VWAP 指标（ATR5/dev_atr/dev_z）。

    指标按 1 分钟时间与 vwap 视图（TTL 15s）对齐合并；vwap 计算不可用时只返回分时。
    """
    try:
        if _exq_unavailable():
            return jsonify({"ok": False, "src": "exhq", "data": [], "error": "exhq unavailable"})
        ttl = int(request.args.get("ttl", "3"))
        key = "fut_exq_minute_%s" % code
        cached = _EXQ_CACHE.get(key)
        if cached and (time.time() - cached[1]) < ttl:
            body = {"ok": True, "src": "exhq", "data": cached[0]}
            if cached[2]:
                body["vwap_params"] = cached[2]
            return jsonify(body)
        data = fut.fut_minute(code)
        # ---- 附带 VWAP 指标：复用 /futures/vwap 视图（含磁盘缓存 + 15s 内存缓存）----
        vwap_params = None
        if data and vws is not None:
            try:
                p = _vwap_eff_params(code, request.args)
                vk = _vwap_cache_key("fut_vwap_", code, p)
                vc = _EXQ_CACHE.get(vk)
                if vc and (time.time() - vc[1]) < 15:
                    vw = vc[0]
                else:
                    vw = vws.build_vwap_view(code, params=p, days=int(p["days"]))
                    _EXQ_CACHE[vk] = (vw, time.time())
                if vw and vw.get("ok") and vw.get("series"):
                    vwap_params = vw.get("params")
                    by_t = {s["t"]: s for s in vw["series"]}
                    for d in data:
                        s = by_t.get(d.get("time"))
                        if s:
                            if s.get("a") is not None:
                                d["atr5"] = s["a"]
                            if s.get("da") is not None:
                                d["dev_atr"] = s["da"]
                            if s.get("z") is not None:
                                d["dev_z"] = s["z"]
            except Exception:
                pass
        _EXQ_CACHE[key] = (data, time.time(), vwap_params)
        body = {"ok": bool(data), "src": "exhq", "data": data}
        if vwap_params:
            body["vwap_params"] = vwap_params
        return jsonify(body)
    except Exception as e:
        return jsonify({"ok": False, "src": "exhq", "data": [], "error": str(e)})


@futures_bp.route("/exquote/quote/<path:code>")
def futures_exquote_quote(code):
    """期指五档盘口 + 持仓/成交量（扩展行情 get_instrument_quote）。"""
    try:
        if _exq_unavailable():
            return jsonify({"ok": False, "src": "exhq", "data": {}, "error": "exhq unavailable"})
        ttl = int(request.args.get("ttl", "3"))
        key = "fut_exq_quote_%s" % code
        cached = _EXQ_CACHE.get(key)
        if cached and (time.time() - cached[1]) < ttl:
            return jsonify({"ok": True, "src": "exhq", "data": cached[0]})
        data = fut.fut_quote(code)
        _EXQ_CACHE[key] = (data, time.time())
        return jsonify({"ok": bool(data), "src": "exhq", "data": data})
    except Exception as e:
        return jsonify({"ok": False, "src": "exhq", "data": {}, "error": str(e)})


@futures_bp.route("/exquote/tick/<path:code>")
def futures_exquote_tick(code):
    """期指逐笔成交（扩展行情 get_transaction_data）。"""
    try:
        if _exq_unavailable():
            return jsonify({"ok": False, "src": "exhq", "data": [], "error": "exhq unavailable"})
        count = int(request.args.get("count", "40"))
        ttl = int(request.args.get("ttl", "3"))
        key = "fut_exq_tick_%s" % code
        cached = _EXQ_CACHE.get(key)
        if cached and (time.time() - cached[1]) < ttl:
            return jsonify({"ok": True, "src": "exhq", "data": cached[0]})
        data = fut.fut_tick(code, count=count)
        _EXQ_CACHE[key] = (data, time.time())
        return jsonify({"ok": bool(data), "src": "exhq", "data": data})
    except Exception as e:
        return jsonify({"ok": False, "src": "exhq", "data": [], "error": str(e)})


@futures_bp.route("/exquote/kline/<path:code>")
def futures_exquote_kline(code):
    """期指历史K线（扩展行情 get_instrument_bars）。period: 1min / 5min / 30min（默认 1min）。"""
    try:
        if _exq_unavailable():
            return jsonify({"ok": False, "src": "exhq", "data": [], "error": "exhq unavailable"})
        period = request.args.get("period", "1min")
        count = int(request.args.get("count", "240"))
        ttl = int(request.args.get("ttl", "15"))
        key = "fut_exq_kline_%s_%s_%d" % (code, period, count)
        cached = _EXQ_CACHE.get(key)
        if cached and (time.time() - cached[1]) < ttl:
            return jsonify({"ok": True, "src": "exhq", "data": cached[0]})
        data = fut.fut_bars(code, category=period, count=count)
        _EXQ_CACHE[key] = (data, time.time())
        return jsonify({"ok": bool(data), "src": "exhq", "data": data})
    except Exception as e:
        return jsonify({"ok": False, "src": "exhq", "data": [], "error": str(e)})


@futures_bp.route("/vwap/<path:code>")
def futures_vwap(code):
    """VWAP 偏离度日内策略视图（看板「VWAP」模式数据源）。

    返回当日 1 分钟序列（价/VWAP/dev_z/量）+ 历史基线统计 + 按报告规则模拟出的
    当日信号（含止损/止盈价位）。参数可选：days / k_long / k_short / rr / atr_mult。
    计算涉及历史 1 分钟分页拉取，故按 code 做 TTL 缓存（默认 15s）。
    """
    try:
        if vws is None:
            return jsonify({"ok": False, "data": None, "error": "vwap_service unavailable"})
        if _exq_unavailable():
            return jsonify({"ok": False, "data": None, "error": "exhq unavailable"})
        try:
            ttl = int(request.args.get("ttl", "3"))
        except Exception:
            ttl = 15
        params = {}
        for k in ("k_long", "k_short", "rr", "atr_mult", "atr_period", "time_stop"):
            v = request.args.get(k)
            if v is not None and v != "":
                try:
                    params[k] = float(v)
                except Exception:
                    pass
        # URL 未显式指定的，用服务端保存的 per-code 参数补齐（days 也走同一套）
        p = _vwap_eff_params(code, request.args)
        for k in ("k_long", "k_short", "rr"):
            if k not in params and k in p:
                params[k] = p[k]
        try:
            days = int(float(request.args.get("days", "") or p.get("days", 30)))
        except Exception:
            days = 30
        if days < 1:
            days = 30
        key = _vwap_cache_key("fut_vwap_", code, dict(params, days=days))
        cached = _EXQ_CACHE.get(key)
        if cached and (time.time() - cached[1]) < ttl:
            body = cached[0]
            body["cached"] = True
            return jsonify(body)
        try:
            data = vws.build_vwap_view(code, params=params or None, days=days)
        except Exception as e:
            return jsonify({"ok": False, "data": None, "error": "calc failed: %s" % e})
        data["cached"] = False
        _EXQ_CACHE[key] = (data, time.time())
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "data": None, "error": str(e)})


@futures_bp.route("/vwap/params", methods=["GET", "POST"])
def futures_vwap_params():
    """VWAP 策略阈值的读写（服务端持久化，供各窗口共享同一口径）。

    GET  ?code=IFL9   -> 该品种参数；不带 code 返回全部
    POST json {code, k_long, k_short, rr, days} -> 保存并返回合并后的参数
    """
    try:
        if vstore is None:
            return jsonify({"ok": False, "error": "params store unavailable"})
        if request.method == "GET":
            code = (request.args.get("code") or "").strip()
            if code:
                return jsonify({"ok": True, "code": code, "data": vstore.get(code)})
            return jsonify({"ok": True, "data": vstore.get_all()})
        body = request.get_json(silent=True) or {}
        code = str(body.get("code") or "").strip()
        if not code:
            return jsonify({"ok": False, "error": "缺少 code"}), 400
        saved = vstore.set_params(code, body)
        # 参数变了，立即作废该品种的所有 VWAP / minute 内存缓存，避免旧阈值继续命中
        try:
            for k in list(_EXQ_CACHE.keys()):
                if isinstance(k, str) and code in k and (k.startswith("fut_vwap_") or k.startswith("fut_exq_minute_")):
                    _EXQ_CACHE.pop(k, None)
        except Exception:
            pass
        return jsonify({"ok": True, "code": code, "data": saved})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@futures_bp.route("/snapshot")
def futures_snapshot():
    """4 个合约实时快照（用于看板头部汇总）。"""
    try:
        if _exq_unavailable():
            return jsonify({"ok": False, "error": "exhq unavailable", "data": []})
        out = []
        for code, name in fut.FUTURES:
            q = fut.fut_quote(code)
            price = q.get("price") or 0
            prev = q.get("last_close") or 0
            chg = price - prev if (price and prev) else 0
            pct = (chg / prev * 100) if (prev and prev != 0) else 0
            out.append({
                "code": code,
                "name": name,
                "price": price,
                "prev_close": prev,
                "change": chg,
                "change_pct": pct,
                "chicang": q.get("chicang") or 0,
                "zongliang": q.get("zongliang") or 0,
                "open": q.get("open") or 0,
                "high": q.get("high") or 0,
                "low": q.get("low") or 0,
                "servertime": q.get("servertime") or "",
            })
        return jsonify({"ok": True, "data": out})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "data": []})


@futures_bp.route("/popup")
def futures_popup():
    """期指综合看盘·独立弹窗页面（4 分时 + 12 K线），供 PyWebView 独立窗口加载。"""
    return render_template("futures_popup.html")


@futures_bp.route("/popup/launch", methods=["POST"])
def futures_popup_launch():
    """期指综合看盘 独立弹窗：A 本机 Popen；局域网返回参数让前端协议唤起 + 降级。"""
    try:
        import webview  # noqa: F401  仅用于确认依赖已安装
    except Exception as e:
        return jsonify({"ok": False, "error": "pywebview 未安装: %s" % e})
    return popup_launch(
        popup_path="/futures/popup",
        title="期指综合看盘 · 独立窗口",
        **POPUP_SIZES["futures"],
        log_name="popup_launcher.log",
    )


@futures_bp.route("/news/data")
def futures_news_data():
    """东方财富 7×24 快讯：代理拉取实时滚动新闻。"""
    try:
        ttl = int(request.args.get("ttl", "20") or "20")
        page_size = int(request.args.get("size", "20") or "20")
        sort_end = request.args.get("sortEnd", "") or ""
        data = ak_service.get_news_fast(ttl=ttl, page_size=page_size, sort_end=sort_end)
        return jsonify({"ok": data.get("ok", False), "error": data.get("error", ""), "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "data": {}})


@futures_bp.route("/news/popup")
def futures_news_popup_page():
    """7×24 快讯·独立窗口页面，供 PyWebView 加载。"""
    return render_template("news_popup.html")


@futures_bp.route("/popup/news/launch", methods=["POST"])
def futures_news_popup_launch():
    """7×24 快讯 独立弹窗：A 本机 Popen；局域网返回参数让前端协议唤起 + 降级。"""
    try:
        import webview  # noqa: F401  仅用于确认依赖已安装
    except Exception as e:
        return jsonify({"ok": False, "error": "pywebview 未安装: %s" % e})
    return popup_launch(
        popup_path="/futures/news/popup",
        title="7×24 快讯 · 独立窗口",
        **POPUP_SIZES["news"],
    )


@futures_bp.route("/capital/popup")
def futures_capital_popup_page():
    """股指资金·独立窗口页面（期指行情 + 资金流），供 PyWebView 加载。"""
    return render_template("capital_popup.html")


@futures_bp.route("/capital/sina")
def futures_capital_sina():
    """代理新浪期货分时数据（JSONP）。"""
    sym = (request.args.get("sym", "") or "").strip()
    if (not sym) or (".." in sym) or ("/" in sym) or (" " in sym) or ("&" in sym) or ("," in sym):
        return jsonify({"ok": False, "error": "invalid sym"}), 400
    target = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t1nf_" + sym +
              "=/InnerFuturesNewService.getMinLine?symbol=" + sym)
    try:
        req = urllib.request.Request(
            target,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read()
        return Response(data, mimetype="application/javascript", headers={"Cache-Control": "no-store"})
    except Exception:
        return Response(b"", status=502, mimetype="application/javascript")


@futures_bp.route("/capital/fflow")
def futures_capital_fflow():
    """代理东方财富资金流分时（push2 fflow kline）。"""
    nid = (request.args.get("nid", "") or "").strip()
    ts = (request.args.get("_", "") or "").strip() or str(int(time.time() * 1000))
    if (not nid) or (".." in nid) or ("/" in nid) or (" " in nid) or ("&" in nid) or ("," in nid):
        return jsonify({"ok": False, "error": "invalid nid"}), 400
    query = ("/api/qt/stock/fflow/kline/get?lmt=0&klt=1"
             "&fields1=f1%2Cf2%2Cf3%2Cf7"
             "&fields2=f51%2Cf52%2Cf53%2Cf54%2Cf55%2Cf56%2Cf57%2Cf58%2Cf59%2Cf60%2Cf61%2Cf62%2Cf63%2Cf64%2Cf65"
             "&ut=b2884a393a59ad64002292a3e90d46a5&secid=" + nid + "&_=" + ts)
    hosts = [
        "https://push2delay.eastmoney.com",
        "https://push2.eastmoney.com",
        "https://push2his.eastmoney.com",
    ]
    last_err = ""
    for host in hosts:
        target = host + query
        try:
            req = urllib.request.Request(
                target,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
            )
            resp = urllib.request.urlopen(req, timeout=10)
            data = resp.read()
            return Response(data, mimetype="application/json", headers={"Cache-Control": "no-store"})
        except Exception as e:
            last_err = "%s: %s" % (host, e)
            continue
    return jsonify({"ok": False, "error": "fflow upstream failed", "detail": last_err}), 502


@futures_bp.route("/popup/capital/launch", methods=["POST"])
def futures_capital_popup_launch():
    """股指资金 独立弹窗：A 本机 Popen；局域网返回参数让前端协议唤起 + 降级。"""
    try:
        import webview  # noqa: F401  仅用于确认依赖已安装
    except Exception as e:
        return jsonify({"ok": False, "error": "pywebview 未安装: %s" % e})
    return popup_launch(
        popup_path="/futures/capital/popup",
        title="股指资金 · 独立窗口",
        **POPUP_SIZES["capital"],
    )


@futures_bp.route("/popup/industry")
def futures_popup_industry():
    """行业板块分布数据（tdx_mac 板块 MAC 协议）：y=涨幅 x=价格。

    type：0=通达信 56 大行业，1=细分行业（约 110 个）。
    涨幅统一由后端按 (price - pre_close) / pre_close * 100 计算。
    """
    try:
        board_type = int(request.args.get("type", "0") or "0")
        if board_type not in (0, 1):
            board_type = 0
        ttl = int(request.args.get("ttl", "30") or "30")
        data = ak_service.get_industry_board_list(board_type=board_type, ttl=ttl)
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "data": []})


@futures_bp.route("/industry/popup")
def futures_industry_popup_page():
    """行业板块分布·独立窗口页面，供 PyWebView 加载。"""
    return render_template("industry_popup.html")


@futures_bp.route("/popup/industry/launch", methods=["POST"])
def futures_industry_popup_launch():
    """行业板块分布 独立弹窗：A 本机 Popen；局域网返回参数让前端协议唤起 + 降级。"""
    try:
        import webview  # noqa: F401  仅用于确认依赖已安装
    except Exception as e:
        return jsonify({"ok": False, "error": "pywebview 未安装: %s" % e})
    return popup_launch(
        popup_path="/futures/industry/popup",
        title="行业板块分布 · 独立窗口",
        **POPUP_SIZES["industry"],
    )


@futures_bp.route("/industry/stocks")
def futures_industry_stocks_page():
    """某行业/板块的成分股分布·独立窗口页面，供 PyWebView 加载（?board=880362&name=渔业）。"""
    board = request.args.get("board", "")
    name = request.args.get("name", "")
    return render_template("industry_stocks_popup.html", board=board, name=name)


@futures_bp.route("/industry/stocks/data")
def futures_industry_stocks_data():
    """某行业/板块的成分股分布数据（MAC 板块成分股 + pytdx 批量报价）。"""
    try:
        board = request.args.get("board", "").strip()
        if not board:
            return jsonify({"ok": False, "error": "缺少 board 参数", "data": []})
        ttl = int(request.args.get("ttl", "15") or "15")
        data = ak_service.get_board_stocks(board, ttl=ttl)
        return jsonify({"ok": True, "board": board, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "data": []})


@futures_bp.route("/popup/industry/stocks/launch", methods=["POST"])
def futures_industry_stocks_launch():
    """板块成分股 独立弹窗（?board=&name=）：A 本机 Popen；局域网返回参数让前端协议唤起 + 降级。"""
    try:
        import webview  # noqa: F401  仅用于确认依赖已安装
    except Exception as e:
        return jsonify({"ok": False, "error": "pywebview 未安装: %s" % e})

    body = request.get_json(silent=True) or {}
    board = str(body.get("board") or "").strip()
    name = str(body.get("name") or "").strip()
    if not board:
        return jsonify({"ok": False, "error": "缺少 board 参数"})
    from urllib.parse import quote
    popup_path = "/futures/industry/stocks?board=%s&name=%s" % (quote(board), quote(name))
    title = "%s · 个股分布" % (name or "板块成分股")
    return popup_launch(
        popup_path=popup_path,
        title=title,
        **POPUP_SIZES["industry_stocks"],
    )


@futures_bp.route("/stock/chart")
def futures_stock_chart_page():
    """个股分时/K线独立窗口页面，供 PyWebView 加载（?code=600519&market=1&name=贵州茅台&pc=1500.0）。"""
    code = request.args.get("code", "")
    market = request.args.get("market", "0")
    name = request.args.get("name", "")
    pc = request.args.get("pc", "0")
    return render_template("stock_chart_popup.html", code=code, market=market, name=name, pc=pc)


@futures_bp.route("/popup/stock/chart/launch", methods=["POST"])
def futures_stock_chart_launch():
    """个股分时/K线 独立弹窗（POST body: code/market/name/pre_close）：A 本机 Popen；局域网返回参数让前端协议唤起 + 降级。"""
    try:
        import webview  # noqa: F401  仅用于确认依赖已安装
    except Exception as e:
        return jsonify({"ok": False, "error": "pywebview 未安装: %s" % e})

    body = request.get_json(silent=True) or {}
    code = str(body.get("code") or "").strip()
    if not code:
        return jsonify({"ok": False, "error": "缺少 code 参数"})
    market = str(body.get("market") or "0").strip()
    name = str(body.get("name") or "").strip()
    pc = str(body.get("pre_close") or "0").strip()
    from urllib.parse import quote
    popup_path = ("/futures/stock/chart?code=%s&market=%s&name=%s&pc=%s"
                  % (quote(code), quote(market), quote(name), quote(pc)))
    title = "%s %s · 分时" % (name or "", code)
    return popup_launch(
        popup_path=popup_path,
        title=title,
        **POPUP_SIZES["stock_chart"],
    )


@futures_bp.route("/popup/overview")
def futures_popup_overview():
    """A 股市场概况：主要指数涨跌、两市涨跌家数、涨停/跌停家数、两市总成交额。"""
    try:
        ttl = int(request.args.get("ttl", "60") or "60")
        data = ak_service.get_market_overview(ttl=ttl)
        return jsonify({"ok": data.get("ok", False), "error": data.get("error", ""), "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "data": {}})


@futures_bp.route("/zhangfu/distribution")
def futures_zhangfu_distribution():
    """全市场涨幅分布（涨跌家数直方图）：东方财富 push2ex getTopicZDFenBu 代理。"""
    try:
        ttl = int(request.args.get("ttl", "30") or "30")
        data = ak_service.get_zhangfu_distribution(ttl=ttl)
        return jsonify({"ok": data.get("ok", False), "error": data.get("error", ""), "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "data": {}})


@futures_bp.route("/zhangfu/popup")
def futures_zhangfu_popup_page():
    """涨幅分布·独立窗口页面，供 PyWebView 加载。"""
    return render_template("zhangfu_popup.html")


@futures_bp.route("/popup/zhangfu/launch", methods=["POST"])
def futures_zhangfu_popup_launch():
    """全市场涨幅分布 独立弹窗：A 本机 Popen；局域网返回参数让前端协议唤起 + 降级。"""
    try:
        import webview  # noqa: F401  仅用于确认依赖已安装
    except Exception as e:
        return jsonify({"ok": False, "error": "pywebview 未安装: %s" % e})
    return popup_launch(
        popup_path="/futures/zhangfu/popup",
        title="全市场涨幅分布 · 独立窗口",
        **POPUP_SIZES["zhangfu"],
    )


@futures_bp.route("/amountflow/data")
def futures_amountflow_data():
    """两市成交分析（上证 + 深证 逐分钟成交额）：pytdx get_index_bars 取值。"""
    try:
        ttl = int(request.args.get("ttl", "120") or "120")
        data = ak_service.get_two_market_turnover(ttl=ttl)
        return jsonify({"ok": data.get("ok", False), "error": data.get("error", ""), "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "data": {}})


@futures_bp.route("/amountflow/popup")
def futures_amountflow_popup_page():
    """两市成交分析·独立窗口页面，供 PyWebView 加载。"""
    return render_template("amountflow_popup.html")


@futures_bp.route("/popup/amountflow/launch", methods=["POST"])
def futures_amountflow_popup_launch():
    """两市成交分析 独立弹窗：A 本机 Popen；局域网返回参数让前端协议唤起 + 降级。"""
    try:
        import webview  # noqa: F401  仅用于确认依赖已安装
    except Exception as e:
        return jsonify({"ok": False, "error": "pywebview 未安装: %s" % e})
    return popup_launch(
        popup_path="/futures/amountflow/popup",
        title="两市成交分析 · 独立窗口",
        **POPUP_SIZES["amountflow"],
    )


@futures_bp.route("/external/popup")
def futures_external_popup_page():
    """外围股市·独立窗口页面（外盘指数直播图），供 PyWebView 加载。"""
    return render_template("external_popup.html")


@futures_bp.route("/external/img")
def futures_external_img():
    """代理东方财富外盘指数直播图。

    由 Flask 后端（具备外网访问能力）拉取图片并转发给 PyWebView 窗口，
    规避 WebView2 直连外网受限/被沙箱隔离导致图片加载失败的问题。
    """
    nid = (request.args.get("nid", "") or "").strip()
    if (not nid) or (".." in nid) or ("/" in nid) or (" " in nid) or ("&" in nid):
        return jsonify({"ok": False, "error": "invalid nid"}), 400
    target = ("https://webquotepic.eastmoney.com/GetPic.aspx?nid=%s"
             "&imageType=rt&_random=%s" % (nid, time.time()))
    try:
        req = urllib.request.Request(
            target,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://quote.eastmoney.com/",
            },
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read()
        ctype = resp.headers.get("Content-Type") or "image/png"
        return Response(data, mimetype=ctype, headers={"Cache-Control": "no-store"})
    except Exception as e:
        return Response(b"", status=502, mimetype="image/png")


@futures_bp.route("/popup/external/launch", methods=["POST"])
def futures_external_popup_launch():
    """外围股市 独立弹窗：A 本机 Popen；局域网返回参数让前端协议唤起 + 降级。"""
    try:
        import webview  # noqa: F401  仅用于确认依赖已安装
    except Exception as e:
        return jsonify({"ok": False, "error": "pywebview 未安装: %s" % e})
    return popup_launch(
        popup_path="/futures/external/popup",
        title="外围股市 · 独立窗口",
        **POPUP_SIZES["external"],
    )


@futures_bp.route("/popup/integrated")
def futures_integrated_popup():
    """综合市场概况·独立窗口页面：集成 行业分布 / 涨幅分布 / 新闻 / 两市成交 / 外围股市 /
       股指资金 / 股指期货 / 期权 等全部子窗口为一个标签页式 PyWebView 窗口，供独立弹窗加载。
    """
    return render_template("integrated_popup.html")


@futures_bp.route("/popup/integrated/launch", methods=["POST"])
def futures_integrated_popup_launch():
    """综合市场概况 独立弹窗：A 本机 Popen；局域网返回参数让前端协议唤起 + 降级。

    集成弹窗一次性弹出包含所有子窗口的标签页，窗口默认采用 POPUP_SIZES["integrated"]。
    """
    try:
        import webview  # noqa: F401  仅用于确认依赖已安装
    except Exception as e:
        return jsonify({"ok": False, "error": "pywebview 未安装: %s" % e})
    size = get_popup_size("integrated")
    return popup_launch(
        popup_path="/futures/popup/integrated",
        title="市场概况 · 综合独立窗口",
        width=size["width"],
        height=size["height"],
        min_w=size["min_w"],
        min_h=size["min_h"],
    )
