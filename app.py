# -*- coding: utf-8 -*-
"""Flask 主入口：注册蓝图 + 启动服务。"""
import logging, os
from datetime import datetime
from flask import Flask, jsonify, render_template

from config import Config
from api.market_bp import market_bp
from api.trade_bp  import trade_bp
from api.cb_bp     import cb_bp


def create_app():
    Config.ensure_dirs()

    # ---- 日志 ----
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(os.path.join(Config.LOG_DIR, "app.log"), encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

    app = Flask(__name__)
    app.config.from_object(Config)

    # ---- 注册蓝图 ----
    app.register_blueprint(market_bp, url_prefix="/market")
    app.register_blueprint(trade_bp,  url_prefix="/trade")
    app.register_blueprint(cb_bp,     url_prefix="/cb")

    # 预热 /cb/full 顶层缓存，避免首屏阻塞（后台线程，启动即算）
    try:
        import threading as _th
        _th.Thread(target=cb_bp._warm_full_cache, daemon=True).start()
    except Exception:
        pass

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/health")
    def health():
        return jsonify({
            "ok": True,
            "service": "xtquant-flask",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    @app.route("/api")
    def endpoints():
        return jsonify({
            "service": "xtquant-flask",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "endpoints": [
                "GET  /",
                "GET  /health",
                "GET  /api",
                "GET  /market/quote/<code>",
                "GET  /market/kline/<code>",
                "GET  /trade/asset",
                "GET  /trade/positions",
                "GET  /trade/orders",
                "POST /trade/order",
                "POST /trade/cancel",
                "GET  /cb/panorama",
                "GET  /cb/top_dual_low",
                "GET  /cb/full",
                "GET  /cb/full/<code>",
                "GET  /cb/full_stats",
            ],
        })

    @app.errorhandler(Exception)
    def handle_err(e):
        logging.exception("unhandled: %s", e)
        return jsonify({"ok": False, "error": type(e).__name__ + ": " + str(e)}), 500

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host=Config.HOST, port=Config.PORT, debug=Config.DEBUG)
