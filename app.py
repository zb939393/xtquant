# -*- coding: utf-8 -*-
"""Flask 主入口：注册蓝图 + 启动服务。"""
import logging, os, signal
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

    @app.route("/cb/watchlist")
    def cb_watchlist():
        # 自选股看板：复用同一前端模板，由前端根据路径进入只看自选模式
        return render_template("index.html")

    @app.route("/cb/watchlist/board")
    def cb_watchlist_board():
        # 自选股看盘：复用同一前端模板，由前端根据路径进入看盘模式（每行4面板）
        return render_template("index.html")

    return app


if __name__ == "__main__":
    app = create_app()

    # 关键修复：改用多线程 WSGI 服务（werkzeug），彻底解决阻塞请求串行化问题。
    # 原方案用 tornado.wsgi.WSGIContainer，所有请求在单个 IOLoop 线程中串行执行；
    # 而 /market/kline、/market/quote 等接口会「同步阻塞」调用 PyTDX
    # （冷缓存约 12~23s），一个慢请求会冻结全部请求，这正是
    # 「日K 正常、切换其它周期卡死/超时」的根因。
    # ak_service 的 PyTDX 连接基于 threading.local 按线程隔离（_PYTDX_POOL），
    # 因此多线程并发调用是安全且预期内的：每个阻塞请求在独立线程执行，互不冻结。
    app.config["TEMPLATES_AUTO_RELOAD"] = True  # 编辑 index.html 后热重载

    logging.getLogger(__name__).info(
        "多线程 WSGI 服务启动: http://%s:%d (DEBUG=%s)",
        Config.HOST, Config.PORT, Config.DEBUG,
    )
    print("Threaded WSGI serving Flask at http://%s:%d" % (Config.HOST, Config.PORT))
    app.run(host=Config.HOST, port=Config.PORT, threaded=True, use_reloader=False, debug=False)
