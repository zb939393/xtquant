# -*- coding: utf-8 -*-
"""PyWebView 独立行情窗口启动器。

用法: python popup_launcher.py [url] [--title 标题] [--width 宽] [--height 高]
打开一个脱离浏览器、无地址栏/导航栏的独立窗口。默认加载期指综合看盘弹窗页面
（4 个分时 + 12 个 K线行情）；也可传 url 加载行业分布等其它页面。
依赖 pywebview，Windows 后端为 WebView2(EdgeChromium)。
"""
import os
import sys


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


def main():
    import argparse
    parser = argparse.ArgumentParser(description="PyWebView 独立行情窗口启动器")
    parser.add_argument("url", nargs="?", default="http://127.0.0.1:5000/futures/popup")
    parser.add_argument("--title", default="期指综合看盘 · 独立窗口")
    parser.add_argument("--width", type=int, default=1560)
    parser.add_argument("--height", type=int, default=1020)
    parser.add_argument("--min-width", type=int, default=200)
    parser.add_argument("--min-height", type=int, default=200)
    args = parser.parse_args()

    try:
        import webview
    except Exception as e:
        print("pywebview 未安装或导入失败: %s" % e)
        return 1

    api = Api()
    # 独立窗口：无地址栏/导航栏；保留系统标题栏便于拖动/最小化/关闭；
    # on_top=True 保证弹窗显示在浏览器等其它窗口前面。
    window = webview.create_window(
        args.title,
        url=args.url,
        width=args.width,
        height=args.height,
        min_size=(args.min_width, args.min_height),
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
