# -*- coding: utf-8 -*-
"""Flask 主入口：注册蓝图 + 启动服务。"""
import logging, os, signal
from datetime import datetime
from flask import Flask, jsonify, render_template
from waitress import serve

from config import Config
from api.market_bp import market_bp
from api.trade_bp  import trade_bp
from api.cb_bp     import cb_bp
from api.option_bp import option_bp
from api.futures_bp import futures_bp


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
    app.register_blueprint(option_bp, url_prefix="/option")
    app.register_blueprint(futures_bp, url_prefix="/futures")

    # ---- 顶层路由：A 股市场概况网页版 ----
    @app.route("/overview")
    def overview():
        return render_template("market_overview.html")

    # 预热顶层缓存，避免首屏阻塞。
    #  /cb/full：后台线程预热，不阻塞启动；
    #  /option/full：后台线程预热，并启动期有限等待（最多 30s）让首份全量数据就绪，
    #               确保期权看板首屏无感（首屏冷加载已由并发化+缓存压缩到约 9s）。
    try:
        import threading as _th
        _th.Thread(target=cb_bp._warm_full_cache, daemon=True).start()
        _opt_warm = _th.Thread(target=option_bp._warm_full_cache, daemon=True)
        _opt_warm.start()
        _opt_warm.join(timeout=30)
        if _opt_warm.is_alive():
            logging.getLogger(__name__).warning(
                "[option/full] 预热未在 30s 内完成，继续后台进行（首屏可能短暂变慢）"
            )
    except Exception:
        pass

    @app.route("/")
    def index():
        # 首页：A 股市场概况（市场概况作为默认首页）
        return render_template("market_overview.html")

    @app.route("/health")
    def health():
        return jsonify({
            "ok": True,
            "service": "xtquant-flask",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    @app.route("/favicon.ico")
    def favicon():
        return "", 204

    @app.route("/api")
    def endpoints():
        return jsonify({
            "service": "xtquant-flask",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "endpoints": [
                "GET  /                    (市场概况首页)",
                "GET  /health",
                "GET  /api",
                "GET  /market/quote/<code>",
                "GET  /market/kline/<code>",
                "GET  /trade/asset",
                "GET  /trade/positions",
                "GET  /trade/orders",
                "POST /trade/order",
                "POST /trade/cancel",
                "GET  /cb/board             (可转债主看板)",
                "GET  /cb/watchlist",
                "GET  /cb/watchlist/board",
                "GET  /cb/panorama",
                "GET  /cb/top_dual_low",
                "GET  /cb/full",
                "GET  /cb/full/<code>",
                "GET  /cb/full_stats",
                "GET  /option/full",
                "GET  /option/full/<code>",
                "GET  /option/full/stats",
                "GET  /futures/board",
                "GET  /futures/snapshot",
                "GET  /futures/exquote/minute/<code>",
                "GET  /futures/exquote/quote/<code>",
                "GET  /futures/exquote/tick/<code>",
                "GET  /futures/popup",
                "POST /futures/popup/launch",
                "GET  /futures/popup/industry",
                "GET  /futures/popup/overview",
                "GET  /overview   (市场概况网页版)",
                "GET  /futures/popup/integrated          (综合市场概况·独立弹窗页面)",
                "POST /futures/popup/integrated/launch    (综合弹窗 PyWebView 启动)",
            ],
        })

    @app.errorhandler(Exception)
    def handle_err(e):
        logging.exception("unhandled: %s", e)
        return jsonify({"ok": False, "error": type(e).__name__ + ": " + str(e)}), 500

    @app.route("/cb/board")
    def cb_board():
        # 可转债主看板（原首页 / 迁移至此）
        return render_template("index.html")

    @app.route("/cb/watchlist")
    def cb_watchlist():
        # 自选股看板：复用同一前端模板，由前端根据路径进入只看自选模式
        return render_template("index.html")

    @app.route("/cb/watchlist/board")
    def cb_watchlist_board():
        # 自选股看盘：复用同一前端模板，由前端根据路径进入看盘模式（每行4面板）
        return render_template("index.html")

    @app.route("/option/board")
    def option_board():
        # 期权监控看板：独立前端模板（复用同一 Vue2/Element-UI/ECharts 技术栈）
        return render_template("option.html")

    @app.route("/futures/board")
    def futures_board():
        # 股指期货监控看板：独立前端模板（复用同一 Vue2/Element-UI/ECharts 技术栈）
        return render_template("futures.html")

    @app.route("/popup-setup")
    def popup_setup():
        """B 机器配置助手：说明如何在本机注册 xtquant-popup:// 协议。

        访问入口：
          A 本机   http://127.0.0.1:5000/popup-setup
          B 局域网 http://A_IP:5000/popup-setup
        """
        from flask import request
        from config import Config
        # 判断访问者来源（A 本机 / B 局域网）
        try:
            from api.popup_helper import is_local_request
            is_local = is_local_request()
        except Exception:
            is_local = request.remote_addr in ("127.0.0.1", "::1")
        # A 服务自身的局域网地址（拼出 B 机器要访问的 URL）
        public_host = (Config.PUBLIC_HOST or request.host.split(":")[0])
        server_url = "http://%s:%d" % (public_host, Config.PORT)
        return render_template(
            "popup_setup.html",
            is_local=is_local,
            remote_addr=request.remote_addr or "",
            server_url=server_url,
        )

    return app


if __name__ == "__main__":
    app = create_app()

    # 关键修复：改用生产级 WSGI 服务 waitress（替代 Flask 内置的 werkzeug 开发服务器，
    # 后者每次启动都会打印 "This is a development server" 警告）。waitress 天然多线程，
    # 每个请求在独立线程执行，能像原 threaded=True 一样撑住并发的 PyTDX 阻塞调用，
    # 且 ak_service 的 PyTDX 连接基于 threading.local 按线程隔离（_PYTDX_POOL），并发安全。
    app.config["TEMPLATES_AUTO_RELOAD"] = True  # 编辑 index.html 后热重载

    logging.getLogger(__name__).info(
        "生产级 WSGI(waitress)服务启动: http://%s:%d (DEBUG=%s, threads=%d)",
        Config.HOST, Config.PORT, Config.DEBUG, 32,
    )
    print("waitress serving Flask at http://%s:%d" % (Config.HOST, Config.PORT))
    serve(app, host=Config.HOST, port=Config.PORT, threads=32)
