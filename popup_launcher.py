# -*- coding: utf-8 -*-
"""PyWebView 独立行情窗口启动器。

两种启动模式：

1. 命令行参数模式（A 机器本机后端 subprocess.Popen 使用）：
   python popup_launcher.py [url] [--title 标题] [--width 宽] [--height 高]
   [--min-width 最小宽] [--min-height 最小高]

2. 自定义协议模式（B 机器浏览器协议唤起使用）：
   popup_launcher.exe "xtquant-popup://?url=<http://A:5000/option/popup>&title=<标题>&w=<宽>&h=<高>"
   launcher 会解析 query 字符串，转成 webview.create_window 参数。

依赖 pywebview，Windows 后端为 WebView2(EdgeChromium)。
"""
import os
import sys
import time
from urllib.parse import urlparse, parse_qs


class Api:
    """暴露给前端 JS 的接口（window.pywebview.api）。"""

    def __init__(self):
        # 窗口句柄在窗口加载完成后绑定（loaded 事件线程内读取原生 Handle 是安全的）
        self._hwnd = None

    def bind_window(self, hwnd):
        self._hwnd = hwnd

    def set_on_top(self, on_top):
        """切换窗口置顶状态：True 置顶，False 取消置顶。

        pywebview 会在线程中调用 js_api，因此不能直接操作 WinForms 控件
        （跨线程会导致窗口未响应/失效）；改用线程安全的 Win32 SetWindowPos
        调整窗口 Z 序。
        """
        if not self._hwnd:
            print("set_on_top: 窗口句柄未就绪")
            return False
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = self._hwnd.ToInt64() if hasattr(self._hwnd, 'ToInt64') else int(self._hwnd)
            # HWND_TOPMOST / HWND_NOTOPMOST 插入到 Z 序顶部/非顶部
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_uint,
            ]
            flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
            insert_after = HWND_TOPMOST if bool(on_top) else HWND_NOTOPMOST
            ok = user32.SetWindowPos(
                wintypes.HWND(hwnd), wintypes.HWND(insert_after),
                0, 0, 0, 0, flags,
            )
            return bool(ok)
        except Exception as e:
            print("set_on_top error: %s" % e)
            return False

    def resize(self, width, height):
        """按内容自适应调整窗口客户区尺寸（Win32 SetWindowPos，线程安全）。

        js_api 在线程中调用，不能直接操作 WinForms 控件；通过 Win32 API
        计算标题栏/边框差值后调整窗口矩形，使客户区恰好等于 width×height。
        """
        if not self._hwnd:
            print("resize: 窗口句柄未就绪")
            return False
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = self._hwnd.ToInt64() if hasattr(self._hwnd, 'ToInt64') else int(self._hwnd)

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            user32 = ctypes.windll.user32
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
            user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
            user32.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_uint,
            ]

            wr = RECT()
            cr = RECT()
            user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(wr))
            user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(cr))
            # 客户区 -> 窗口矩形（含标题栏/边框）的差值
            dx = (wr.right - wr.left) - (cr.right - cr.left)
            dy = (wr.bottom - wr.top) - (cr.bottom - cr.top)

            SWP_NOMOVE = 0x0002
            SWP_NOZORDER = 0x0004
            ok = user32.SetWindowPos(
                wintypes.HWND(hwnd), 0,
                0, 0, int(width) + dx, int(height) + dy, SWP_NOMOVE | SWP_NOZORDER,
            )
            return bool(ok)
        except Exception as e:
            print("resize error: %s" % e)
            return False


def _msgbox(title, text, icon=0):
    """原生 Windows 弹窗。GUI 进程（--noconsole）下 print 没地方显示，必须用 MessageBox。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, icon)
    except Exception:
        pass


def _log_line(text):
    """写一行到 popup_launcher.log + 尝试 print（console 模式可见，GUI 模式静默）。"""
    try:
        log_path = os.path.join(os.path.expanduser("~"), ".xtquant", "popup_launcher.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:
        pass
    try:
        print(text)
    except Exception:
        pass


def _cli_register(cmd):
    """命令行协议注册/注销。无需 pyinstaller 也能跑（pywin32/winreg 是 stdlib）。

    GUI 进程（pyinstaller --noconsole）下 print 没控制台显示，
    所以这里用 ctypes.MessageBoxW 弹原生 Windows 弹窗 + 写文件日志双保险。
    """
    if sys.platform != "win32":
        _msgbox("xtquant-popup", "Only supported on Windows")
        return False
    try:
        import winreg
    except ImportError:
        _msgbox("xtquant-popup", "winreg module not available", icon=16)  # 16=error icon
        return False
    base = r"Software\Classes\xtquant-popup"
    exe = os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__)
    if not getattr(sys, 'frozen', False):
        cmdline = '"%s" "%s" "%%1"' % (sys.executable, os.path.abspath(__file__))
    else:
        cmdline = '"%s" "%%1"' % exe

    if cmd == "--register-status":
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as k:
                val, _ = winreg.QueryValueEx(k, None)
                msg = "[xtquant-popup] OK registered:\n\n%s\n\n(written to HKCU)" % val
                _log_line(msg)
                _msgbox("xtquant-popup · status", msg)
                return True
        except FileNotFoundError:
            msg = "[xtquant-popup] NOT registered.\n\nRun with --register to register."
            _log_line(msg)
            _msgbox("xtquant-popup · status", msg, icon=48)  # 48=warning icon
            return False
        except Exception as e:
            msg = "[xtquant-popup] query error: %s" % e
            _log_line(msg)
            _msgbox("xtquant-popup · status", msg, icon=16)
            return False

    if cmd == "--unregister":
        try:
            _delete_key_recursive(winreg.HKEY_CURRENT_USER, base)
            msg = "[xtquant-popup] unregistered from HKCU"
            _log_line(msg)
            _msgbox("xtquant-popup", msg)
            return True
        except FileNotFoundError:
            msg = "[xtquant-popup] nothing to unregister"
            _log_line(msg)
            _msgbox("xtquant-popup", msg, icon=48)
            return True
        except Exception as e:
            msg = "[xtquant-popup] unregister failed: %s" % e
            _log_line(msg)
            _msgbox("xtquant-popup", msg, icon=16)
            return False

    if cmd == "--register":
        # 用 KEY_SET_VALUE 权限（不要 ALL_ACCESS），键已存在时也能写
        access = winreg.KEY_SET_VALUE
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, base, 0, access) as k:
                winreg.SetValueEx(k, None, 0, winreg.REG_SZ, "URL:xtquant-popup Protocol")
                winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, base + r"\DefaultIcon", 0, access) as k:
                winreg.SetValueEx(k, None, 0, winreg.REG_SZ, '"%s",0' % exe)
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command", 0, access) as k:
                winreg.SetValueEx(k, None, 0, winreg.REG_SZ, cmdline)
            msg = ("[xtquant-popup] REGISTERED\n\n"
                   "Handler: %s\n\n"
                   "Now you can use xtquant-popup:// links in any browser.\n"
                   "(No admin required; written to HKCU)" % cmdline)
            _log_line(msg)
            _msgbox("xtquant-popup", msg)
            return True
        except Exception as e:
            msg = ("[xtquant-popup] REGISTER FAILED\n\n%s\n\n"
                   "Try running this exe as Administrator, or run --unregister first." % e)
            _log_line(msg)
            _msgbox("xtquant-popup", msg, icon=16)
            return False
    return False


def _delete_key_recursive(root, sub_key):
    """递归删除注册表键。stdlib 没有原生 API；用 _winreg 内部枚举 + 删除。"""
    import winreg
    try:
        with winreg.OpenKey(root, sub_key, 0, winreg.KEY_ALL_ACCESS) as parent:
            while True:
                try:
                    child = winreg.EnumKey(parent, 0)
                except OSError:
                    break
                _delete_key_recursive(root, sub_key + "\\" + child)
        winreg.DeleteKey(root, sub_key)
    except FileNotFoundError:
        pass


def main():
    # === 维护命令：--register / --unregister ===
    # 单独运行：popup_launcher.exe --register  → 把本 exe 注册为 xtquant-popup:// 协议 handler（HKCU，无需管理员）
    # 单独运行：popup_launcher.exe --unregister → 注销协议
    # 单独运行：popup_launcher.exe --register-status → 打印是否已注册
    if len(sys.argv) > 1 and sys.argv[1].lower() in ("--register", "--unregister", "--register-status"):
        _cli_register(sys.argv[1].lower())
        return 0

    # 模式 2：自定义协议唤起（xtquant-popup://?url=...&title=...&w=...&h=...）
    # 浏览器/系统把 xtquant-popup:// 链接转交给本 launcher，第一个参数就是完整 URL。
    if len(sys.argv) > 1 and sys.argv[1].lower().startswith("xtquant-popup:"):
        proto_url = sys.argv[1]
        q = parse_qs(urlparse(proto_url).query)
        url = (q.get("url") or ["http://127.0.0.1:5000/futures/popup"])[0]
        title = (q.get("title") or ["期指综合看盘 · 独立窗口"])[0]
        try:
            width = int((q.get("w") or ["1240"])[0])
        except (TypeError, ValueError):
            width = 1240
        try:
            height = int((q.get("h") or ["700"])[0])
        except (TypeError, ValueError):
            height = 700
        try:
            min_width = int((q.get("minw") or ["200"])[0])
        except (TypeError, ValueError):
            min_width = 200
        try:
            min_height = int((q.get("minh") or ["200"])[0])
        except (TypeError, ValueError):
            min_height = 200
    else:
        # 模式 1：命令行参数
        import argparse
        parser = argparse.ArgumentParser(description="PyWebView 独立行情窗口启动器")
        parser.add_argument("url", nargs="?", default="http://127.0.0.1:5000/futures/popup")
        parser.add_argument("--title", default="期指综合看盘 · 独立窗口")
        parser.add_argument("--width", type=int, default=1560)
        parser.add_argument("--height", type=int, default=1020)
        parser.add_argument("--min-width", type=int, default=200)
        parser.add_argument("--min-height", type=int, default=200)
        a = parser.parse_args()
        url, title, width, height, min_width, min_height = (
            a.url, a.title, a.width, a.height, a.min_width, a.min_height
        )

    try:
        import webview
    except Exception as e:
        print("pywebview 未安装或导入失败: %s" % e)
        return 1

    api = Api()
    # 独立窗口：无地址栏/导航栏；保留系统标题栏便于拖动/最小化/关闭；
    # on_top=True 保证弹窗显示在浏览器等其它窗口前面。
    window = webview.create_window(
        title,
        url=url,
        width=width,
        height=height,
        min_size=(min_width, min_height),
        resizable=True,
        text_select=True,
        on_top=True,
        js_api=api,
    )

    # 窗口加载完成后捕获原生句柄，并主动置顶（确保显示在浏览器前面）
    def on_loaded():
        try:
            api.bind_window(window.native.Handle)
            api.set_on_top(True)
            print("bound window hwnd:", api._hwnd)
        except Exception as e:
            print("bind window handle error: %s" % e)

    window.events.loaded += on_loaded
    # private_mode=False + 固定 storage_path：所有独立弹窗共享同一 WebView2 用户
    # 数据目录，localStorage 可跨窗口/跨进程持久化（否则默认 private_mode=True
    # 每次启动用临时目录，关闭重开后缩放等设置会丢失）。
    webview.start(
        debug=False,
        private_mode=False,
        storage_path=os.path.join(os.path.expanduser("~"), ".xtquant", "webview_data"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
