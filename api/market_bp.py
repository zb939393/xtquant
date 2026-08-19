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
    """日K：固定走 PyTDX（最可靠）。miniQMT/xtdata 不可用，不再尝试。"""
    count = int(request.args.get("count", "120"))
    period = request.args.get("period", "1d")
    data = []
    try:
        data = ak_service.get_kline_pytdx(code, count=count, period=period)
    except Exception:
        data = []
    return jsonify({"ok": bool(data), "count": len(data), "src": "pytdx", "period": period, "data": data})
