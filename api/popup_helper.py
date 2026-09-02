# -*- coding: utf-8 -*-
"""弹窗启动辅助：统一处理『本机 A 直接 Popen PyWebView』与『局域网 B 返回参数让前端协议唤起』。

- A 机器（服务器本机）点击按钮：request.remote_addr 是 127.0.0.1/::1（或 Config.LAN_LOCAL_IPS 配置的局域网 IP），
  行为兼容旧版——后端直接 subprocess.Popen 启动 popup_launcher.py，弹窗出现在 A 桌面上。
- B 机器（局域网其他电脑）点击按钮：request.remote_addr 是局域网 IP，
  后端不再 Popen（避免在没人看的 A 桌面弹窗），返回
      {"ok": True, "action": "client_launch", "url": ..., "title": ..., "w": ..., "h": ...}
  由前端尝试用 xtquant-popup:// 协议唤起 B 本地 PyWebView 客户端（如果装了），未唤起则降级 window.open。
- B 客户端未装/未注册协议：浏览器不会失焦，前端 800ms 内监听到『没唤起』，降级为新标签页打开 URL。
"""
import os
import subprocess
import sys

from flask import jsonify, request

from config import Config


def is_local_request(req=None):
    """判断请求是否来自『服务器本机 A』。

    本机 = remote_addr 是 127.0.0.1 / ::1，或在 Config.LAN_LOCAL_IPS 列表里（A 可能被自身用
    局域网 IP 访问，譬如 B 转发回流，但更常见的是开发者本机用 192.168.x.x 访问自己的 A 机器——
    这种情况按本机处理仍合理，因为 Popen 出来的弹窗就在 A 桌面上、与浏览器同机可见）。
    """
    req = req or request
    addr = (req.remote_addr or "").strip()
    if not addr:
        return False
    if addr in ("127.0.0.1", "::1"):
        return True
    extra = getattr(Config, "LAN_LOCAL_IPS", []) or []
    if addr in extra:
        return True
    return False


def _build_remote_url(req, popup_path):
    """构造 B 弹窗要访问的完整 URL：http://<A 局域网 IP>:<port><popup_path>。

    优先 Config.PUBLIC_HOST（推荐用户在 config.py 显式填 A 的局域网 IP/域名）。
    兜底：取 req.host（B 访问时使用的 host 头，形如 192.168.1.10:5000），
    去掉可能带有的 :port 后再拼 Config.PORT，保证 B 弹窗去 A:5000 取数据。
    """
    if Config.PUBLIC_HOST:
        host = Config.PUBLIC_HOST
        port = Config.PORT
    else:
        host = (req.host or "").split(":")[0]
        port = Config.PORT
    return "http://%s:%d%s" % (host, port, popup_path)


def _popen_local_launcher(url, title, width, height, min_w, min_h, log_name):
    """在本机 A 派生独立 Python 进程运行 popup_launcher.py（兼容旧行为）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, "popup_launcher.py")
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    log_path = os.path.join(Config.LOG_DIR, log_name or "popup_launcher.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        subprocess.Popen(
            [sys.executable, script, url,
             "--title", title,
             "--width", str(width), "--height", str(height),
             "--min-width", str(min_w), "--min-height", str(min_h)],
            creationflags=flags,
            stdout=f,
            stderr=f,
            close_fds=True,
        )


def popup_launch(popup_path, title, width, height, min_w=200, min_h=200, log_name="popup_launcher.log"):
    """统一入口：判断请求来源，本机 Popen，局域网返回参数。

    参数：
        popup_path: 形如 "/option/popup" 或 "/futures/amountflow/popup" 的相对路径。
        title/width/height/min_w/min_h: 弹窗参数。
        log_name: 本机启动时日志文件名（相对 Config.LOG_DIR）。

    返回 Flask Response（jsonify）。
    """
    import logging
    _log = logging.getLogger("popup_helper")
    try:
        is_local = is_local_request()
        _log.info("popup_launch: path=%s remote_addr=%s host=%s is_local=%s",
                  popup_path, request.remote_addr, request.host, is_local)
        if is_local:
            # 兼容：A 本机仍用 127.0.0.1 访问自己的服务，弹窗在 A 桌面弹出。
            url = "http://127.0.0.1:%d%s" % (Config.PORT, popup_path)
            _popen_local_launcher(url, title, width, height, min_w, min_h, log_name)
            return jsonify({"ok": True, "action": "local_started", "url": url,
                            "title": title, "w": width, "h": height})
        # 局域网 B 访问：不 Popen（避免在没人看的 A 桌面弹窗），
        # 返回参数让前端尝试协议唤起 + 降级。
        remote_url = _build_remote_url(request, popup_path)
        _log.info("popup_launch: client_launch -> %s", remote_url)
        return jsonify({"ok": True, "action": "client_launch",
                        "url": remote_url, "title": title, "w": width, "h": height})
    except Exception as e:
        _log.exception("popup_launch error: %s", e)
        return jsonify({"ok": False, "error": str(e)})
