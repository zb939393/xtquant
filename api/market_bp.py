# -*- coding: utf-8 -*-
"""行情蓝图：/market/quote /market/kline"""
from flask import Blueprint, jsonify, request
from core import ak_service

market_bp = Blueprint("market", __name__)


@market_bp.route("/quote/<path:code>")
def quote(code):
    """实时行情走 akshare（miniQMT 不可用，固定回退）。"""
    data = ak_service.stock_spot(code)
    return jsonify({"ok": bool(data), "src": "akshare", "data": data})


@market_bp.route("/kline/<path:code>")
def kline(code):
    """日K：固定走 PyTDX（最可靠）。miniQMT/xtdata 不可用，不再尝试。

    adjust: ''（原始）/'qfq'（前复权）/'hfq'（后复权）。
    """
    count = int(request.args.get("count", "120"))
    period = request.args.get("period", "1d")
    adjust = request.args.get("adjust", "")
    if adjust not in ("qfq", "hfq"):
        adjust = ""
    data = []
    try:
        data = ak_service.get_kline_pytdx(code, count=count, period=period, adjust=adjust)
    except Exception:
        data = []
    return jsonify({"ok": bool(data), "count": len(data), "src": "pytdx",
                    "period": period, "adjust": adjust, "data": data})


@market_bp.route("/minute/<path:code>")
def minute(code):
    """当日分时（1 分钟时间分享线）：PyTDX get_minute_time_data。"""
    ttl = int(request.args.get("ttl", "10"))
    data = []
    try:
        data = ak_service.get_minute_time_data_pytdx(code, ttl=ttl)
    except Exception:
        data = []
    return jsonify({"ok": bool(data), "count": len(data), "src": "pytdx", "data": data})


@market_bp.route("/depth/<path:code>")
def depth(code):
    """五档买卖盘口：PyTDX get_security_quotes。"""
    ttl = int(request.args.get("ttl", "3"))
    data = {}
    try:
        data = ak_service.get_quote_pytdx(code, ttl=ttl)
    except Exception:
        data = {}
    return jsonify({"ok": bool(data), "src": "pytdx", "data": data})


@market_bp.route("/tick/<path:code>")
def tick(code):
    """逐笔成交：PyTDX get_transaction_data。"""
    count = int(request.args.get("count", "40"))
    ttl = int(request.args.get("ttl", "3"))
    data = []
    try:
        data = ak_service.get_transaction_pytdx(code, count=count, ttl=ttl)
    except Exception:
        data = []
    return jsonify({"ok": bool(data), "count": len(data), "src": "pytdx", "data": data})
