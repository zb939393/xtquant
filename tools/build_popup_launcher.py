# -*- coding: utf-8 -*-
"""构建 popup_launcher.exe 并在 Windows 上注册 xtquant-popup:// 自定义协议。

用法（在项目根目录）：
    python tools/build_popup_launcher.py
    # 或带可选参数：
    python tools/build_popup_launcher.py --no-register   # 只打包，不写注册表
    python tools/build_popup_launcher.py --register-only # 不打包，只注册已存在的 dist\\popup_launcher.exe

依赖：pip install pyinstaller
产物：dist\\popup_launcher.exe（单文件，包含 pywebview 依赖）
      注册表：HKEY_CURRENT_USER\\Software\\Classes\\xtquant-popup
              （无需管理员权限，写到 HKCU；多用户互不影响）

协议格式：xtquant-popup://?url=<http://A:5000/...>&title=<标题>&w=<宽>&h=<高>&minw=200&minh=200
"""
import argparse
import os
import shutil
import subprocess
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LAUNCHER = os.path.join(ROOT, "popup_launcher.py")
DIST = os.path.join(ROOT, "dist")
EXE_PATH = os.path.join(DIST, "popup_launcher.exe")
PROTOCOL_NAME = "xtquant-popup"


def run_pyinstaller():
    """调用 pyinstaller 打包 popup_launcher.py 为单文件 exe。"""
    if not os.path.isfile(LAUNCHER):
        print("[!] 找不到入口文件: %s" % LAUNCHER)
        return False
    if not _has_pyinstaller():
        print("[!] 未安装 pyinstaller，先 pip install pyinstaller")
        return False
    # 清理旧产物
    if os.path.isdir(os.path.join(ROOT, "build")):
        shutil.rmtree(os.path.join(ROOT, "build"), ignore_errors=True)
    pyi_cache = os.path.join(ROOT, "__pycache__")
    if os.path.isdir(pyi_cache):
        shutil.rmtree(pyi_cache, ignore_errors=True)
    print("[*] 运行 pyinstaller --onefile ...")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile",
        "--name", "popup_launcher",
        # popup_launcher 是个独立 GUI 程序，不需要控制台窗口
        "--noconsole",
        # 资源文件：templates/static 打包进去（pywebview 加载 URL 时会用到）
        # 实际上 launcher 只加载 URL 不读本地文件，所以无需 --add-data
        LAUNCHER,
    ]
    p = subprocess.run(cmd, cwd=ROOT)
    if p.returncode != 0:
        print("[!] pyinstaller 失败，returncode=%d" % p.returncode)
        return False
    if not os.path.isfile(EXE_PATH):
        print("[!] 期望产物不存在: %s" % EXE_PATH)
        return False
    print("[OK] 已生成 %s (%.1f MB)" % (EXE_PATH, os.path.getsize(EXE_PATH) / 1024 / 1024))
    return True


def _has_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
        return True
    except Exception:
        return False


def register_windows_protocol(exe_path=None, name=PROTOCOL_NAME):
    """把 xtquant-popup:// 协议写到 HKCU\Software\Classes（无需管理员）。"""
    if sys.platform != "win32":
        print("[!] 非 Windows 平台，跳过注册表写入。请手动配置 OS 协议关联。")
        return False
    exe = exe_path or EXE_PATH
    if not os.path.isfile(exe):
        print("[!] 找不到 exe: %s  （请先 --no-register=false 走打包步骤，或 --register-only 时手动传 --exe-path）" % exe)
        return False
    try:
        import winreg
    except ImportError:
        print("[!] 缺少 winreg 模块")
        return False

    # 注册表项设计：
    # HKCU\Software\Classes\xtquant-popup             (默认) = "URL:xtquant-popup Protocol"
    # HKCU\Software\Classes\xtquant-popup             URL Protocol = ""
    # HKCU\Software\Classes\xtquant-popup\shell\open\command
    #                                                       (默认) = "C:\path\popup_launcher.exe" "%1"
    base = r"Software\Classes\%s" % name
    cmd = '"%s" "%%1"' % exe
    try:
        # 根项
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, "URL:xtquant-popup Protocol")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        # icon（可选，用 exe 自带图标）
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\DefaultIcon") as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, '"%s",0' % exe)
        # shell\open\command
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, cmd)
        print("[OK] 已注册协议: %s:// -> %s" % (name, exe))
        print("     （写入 HKCU，无需管理员权限）")
        return True
    except Exception as e:
        print("[!] 注册表写入失败: %s" % e)
        return False


def main():
    ap = argparse.ArgumentParser(description="打包 popup_launcher.exe 并注册 xtquant-popup:// 协议")
    ap.add_argument("--no-register", action="store_true", help="只打包，不写注册表")
    ap.add_argument("--register-only", action="store_true", help="不打包，只注册已存在的 exe")
    ap.add_argument("--exe-path", help="--register-only 时手动指定 exe 路径")
    args = ap.parse_args()

    if not args.register_only:
        if not run_pyinstaller():
            sys.exit(1)
    if not args.no_register:
        ok = register_windows_protocol(exe_path=args.exe_path)
        if not ok:
            sys.exit(1)
    print("\n[完成] 下一步：把 dist\\popup_launcher.exe 拷贝到 B 机器任意位置，B 机器双击运行一次即可完成协议注册。")
    print("       在 B 机器浏览器访问 http://A:5000 即可点『独立弹窗』按钮唤起本地弹窗。")


if __name__ == "__main__":
    main()
