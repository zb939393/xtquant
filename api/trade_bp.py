# -*- coding: utf-8 -*-
"""交易蓝图：/trade/asset /positions /orders /order /cancel"""
from flask import Blueprint, jsonify, request
from core.xt_service import get_trader

trade_bp = Blueprint("trade", __name__)


@trade_bp.route("/asset")
def asset():
    return jsonify({"ok": True, "data": get_trader().asset()})


@trade_bp.route("/positions")
def positions():
    return jsonify({"ok": True, "data": get_trader().positions()})


@trade_bp.route("/orders")
def orders():
    return jsonify({"ok": True, "data": get_trader().orders()})


@trade_bp.route("/trades")
def trades():
    return jsonify({"ok": True, "data": get_trader().trades()})


@trade_bp.route("/order", methods=["POST"])
def order():
    body = request.get_json(force=True)
    t = body.get("order_type", "buy")
    code = body["stock_code"]; vol = int(body["volume"]); price = float(body["price"])
    tr = get_trader()
    if t == "buy":
        ret = tr.buy(code, vol, price, name=body.get("strategy_name", "flask"))
    else:
        ret = tr.sell(code, vol, price, name=body.get("strategy_name", "flask"))
    return jsonify({"ok": True, "data": ret})


@trade_bp.route("/cancel", methods=["POST"])
def cancel():
    body = request.get_json(force=True)
    return jsonify({"ok": True, "data": get_trader().cancel(body["orderid"])})
