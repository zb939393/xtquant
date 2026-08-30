# -*- coding: utf-8 -*-
"""PyWebView 独立行情窗口启动器。

用法: python popup_launcher.py [url] [--title 标题] [--width 宽] [--height 高]
打开一个脱离浏览器、无地址栏/导航栏的独立窗口。默认加载期指综合看盘弹窗页面
（4 个分时 + 12 个 K线行情）；也可传 url 加载行业分布等其它页面。
依赖 pywebview，Windows 后端为 WebView2(EdgeChromium)。
"""
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


def main():
    import argparse
    parser = argparse.ArgumentParser(description="PyWebView 独立行情窗口启动器")
    parser.add_argument("url", nargs="?", default="http://127.0.0.1:5000/futures/popup")
    parser.add_argument("--title", default="期指综合看盘 · 独立窗口")
    parser.add_argument("--width", type=int, default=1560)
    parser.add_argument("--height", type=int, default=1020)
    parser.add_argument("--min-width", type=int, default=1024)
    parser.add_argument("--min-height", type=int, default=720)
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
    webview.start(debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
