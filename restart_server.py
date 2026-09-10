# -*- coding: utf-8 -*-
"""Flask 服务「单实例」启动 / 重启助手。

背景
----
Windows 下 socket 默认带 SO_REUSEADDR，两个进程可以同时 bind 同一端口（后者"劫持"
前者），waitress 不会报 "Address already in use"。于是反复 `python app.py` 会悄悄堆出
多个实例，请求随机落到某个实例上 —— 表现为"改了代码没生效 / 数据时有时无 / 间歇性超时"。

本脚本做三件事：
  1) 找出当前监听目标端口的全部进程并终止（含子孙进程），等到端口真正释放；
  2) 以单实例方式启动 app.py（自动带上用户 site-packages 的 PYTHONPATH）；
  3) 校验端口上**只剩一个** LISTEN，并打印监听进程 PID。

用法
----
    python restart_server.py            # 重启（先杀旧，再起新）
    python restart_server.py --status   # 只查看当前监听情况，不做任何变更
    python restart_server.py --stop     # 只停止，不启动
"""
from __future__ import annotations

import argparse
import os
import site
import socket
import subprocess
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

LOG_DIR = os.path.join(ROOT, "logs")


def _target():
    """从 config.Config 读取 HOST/PORT，读不到就用默认值。"""
    try:
        from config import Config
        return getattr(Config, "HOST", "0.0.0.0"), int(getattr(Config, "PORT", 5000))
    except Exception:
        return "0.0.0.0", 5000


def _listeners(port: int):
    """返回监听指定端口的 (pid, name, cmdline) 列表。"""
    out = []
    try:
        import psutil
    except Exception:
        return out
    me = os.getpid()
    for c in psutil.net_connections(kind="tcp"):
        try:
            if not c.laddr or c.laddr.port != port or c.status != psutil.CONN_LISTEN:
                continue
            pid = c.pid
            if pid is None or pid == me or pid == 0:
                continue
            try:
                pr = psutil.Process(pid)
                cmd = " ".join(pr.cmdline())
                name = pr.name()
            except Exception:
                name, cmd = "?", ""
            if all(pid != p for p, _, _ in out):
                out.append((pid, name, cmd))
        except Exception:
            continue
    return out


def _port_open(port: int, timeout: float = 1.0) -> bool:
    """端口是否有进程在 LISTEN（用 psutil 判断，规避本机 socket.connect_ex 缺失的异常）。"""
    try:
        import psutil
    except Exception:
        return False
    try:
        for c in psutil.net_connections(kind="tcp"):
            try:
                if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN:
                    return True
            except Exception:
                continue
    except Exception:
        return False
    return False


def show_status(port: int):
    ls = _listeners(port)
    if not ls:
        print("[status] 端口 %d：无监听进程（服务未运行）" % port)
        return ls
    print("[status] 端口 %d：发现 %d 个监听进程" % (port, len(ls)))
    for pid, name, cmd in ls:
        print("         pid=%-6s %-12s %s" % (pid, name, cmd[:110]))
    if len(ls) > 1:
        print("         ⚠️  存在重复实例，请在项目根目录执行： python restart_server.py")
    return ls


def stop_all(port: int, wait: float = 10.0) -> bool:
    try:
        import psutil
    except Exception as e:
        print("[stop] 需要 psutil：%s" % e)
        return False
    ls = _listeners(port)
    if not ls:
        print("[stop] 端口 %d 无监听进程，无需停止" % port)
        return True
    for pid, name, cmd in ls:
        try:
            pr = psutil.Process(pid)
            for ch in pr.children(recursive=True):
                try:
                    ch.terminate()
                except Exception:
                    pass
            pr.terminate()
            print("[stop] 已发送终止信号 pid=%s (%s)" % (pid, cmd[:80]))
        except Exception as e:
            print("[stop] 终止 pid=%s 失败：%s" % (pid, e))
    deadline = time.time() + wait
    while time.time() < deadline:
        if not _port_open(port, 0.6):
            print("[stop] 端口 %d 已释放" % port)
            return True
        time.sleep(0.4)
    # 仍未释放 → 强杀
    for pid, _, _ in _listeners(port):
        try:
            psutil.Process(pid).kill()
            print("[stop] 强制结束 pid=%s" % pid)
        except Exception:
            pass
    time.sleep(1.0)
    if _port_open(port, 0.6):
        print("[stop] ✗ 端口 %d 仍被占用" % port)
        return False
    print("[stop] 端口 %d 已释放（强杀后）" % port)
    return True


def start_one(port: int):
    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOG_DIR, "restart_%s.log" % stamp)
    env = dict(os.environ)
    # 自管 Python 上 site.getusersitepackages() 常返回错误路径（缺 AppData\Roaming），
    # 而沙箱可能剥离 APPDATA 等环境变量。因此多路径候选 + 逐项校验「该目录真有 akshare」
    # 才纳入 PYTHONPATH，避免启动后 import akshare 失败。
    _candidates = []
    home = os.path.expanduser("~")
    if home and home != "~":
        _candidates.append(os.path.join(home, "AppData", "Roaming", "Python", "Python313", "site-packages"))
    _appdata = os.environ.get("APPDATA", "")
    if _appdata:
        _candidates.append(os.path.join(_appdata, "Python", "Python313", "site-packages"))
    # 已知本机 akshare 实际目录（硬编码兜底，确保在剥离环境变量的沙箱里也能启动）
    _candidates.append(r"C:\Users\zb\AppData\Roaming\Python\Python313\site-packages")
    try:
        _usp = site.getusersitepackages()
        if _usp:
            _candidates.append(_usp)
    except Exception:
        pass
    existing = env.get("PYTHONPATH", "")
    for _sp in _candidates:
        if not _sp or _sp in existing:
            continue
        # 归一化分隔符，且必须确实包含 akshare 包才采用
        _norm = _sp.replace("/", "\\")
        if os.path.isdir(_norm) and os.path.isdir(os.path.join(_norm, "akshare")):
            existing = _norm + os.pathsep + existing
    if existing:
        env["PYTHONPATH"] = existing.rstrip(os.pathsep)
    fh = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "app.py"],
        cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    print("[start] 已启动 app.py pid=%s，日志：%s" % (proc.pid, log_path))
    return proc, log_path


def verify(port: int, timeout: float = 40.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(port, 0.8):
            break
        time.sleep(0.5)
    ls = show_status(port)
    if len(ls) == 1:
        print("[verify] ✓ 单实例运行正常")
        return True
    if not ls:
        print("[verify] ✗ 端口无监听，启动可能失败，请查看上面日志文件")
        return False
    print("[verify] ✗ 仍有 %d 个实例" % len(ls))
    return False


def main():
    ap = argparse.ArgumentParser(description="Flask 单实例启动/重启助手")
    ap.add_argument("--status", action="store_true", help="只查看监听状态")
    ap.add_argument("--stop", action="store_true", help="只停止服务")
    args = ap.parse_args()
    host, port = _target()
    print("[cfg] 目标地址 http://%s:%d" % (host, port))

    if args.status:
        show_status(port)
        return 0
    if args.stop:
        return 0 if stop_all(port) else 1

    if not stop_all(port):
        print("[abort] 旧实例未清理干净，已中止启动")
        return 1
    start_one(port)
    return 0 if verify(port) else 1


if __name__ == "__main__":
    sys.exit(main())
