# 局域网弹窗：让 B 电脑也能点『独立弹窗』

## 背景

- **A 机器（服务器）**：跑 `app.py`（waitress 监听 `0.0.0.0:5000`），是数据源（ak_share、xtquant、PyTDX 等）。
- **B 机器（局域网其他电脑）**：用浏览器访问 `http://A:5000/...` 看板/看盘。
- **「独立弹窗」按钮**：A 上点击 → 后端直接 `subprocess.Popen` 启动 PyWebView 窗口，弹在 A 桌面；B 上点击如果不改造就只能弹在 A 桌面（无人在看），B 用户看不到。

## 方案概览

| 场景 | 后端行为 | 前端行为 |
|------|----------|----------|
| **A 本机** (`127.0.0.1`) 点按钮 | `subprocess.Popen` 直接拉起 `popup_launcher.py`，弹窗出现在 A 桌面 | 仅显示成功消息 |
| **B 局域网** (`192.168.x.x`) 点按钮，**装了客户端** | 后端不 Popen，返回 `{ok, action:'client_launch', url, title, w, h}` | 触发 `xtquant-popup://?url=...` 协议，B 浏览器把链接交给 B 本地 launcher → B 桌面弹窗 |
| **B 局域网** 点按钮，**没装客户端** | 同上，返回 `client_launch` 参数 | 800ms 内未失焦 → 降级 `window.open(url, '_blank')` 在 B 浏览器新标签打开弹窗 URL |

## A 机器（服务器）配置

### 1. 设置本机局域网 IP（推荐）

编辑 `config.py`：

```python
class Config:
    ...
    # A 机器的局域网 IP（B 机器访问时使用的地址）
    PUBLIC_HOST = "192.168.1.10"   # ← 改成你的 A 机器 IP
    # 留空则从 request.host 推断（兜底，但显式填更稳）
```

### 2. 确认后端监听所有网卡

`config.py` 中 `HOST = "0.0.0.0"`（默认就是 0.0.0.0，✅ 不用改）。

### 3. 打包 B 用的客户端（pyinstaller）

**前置**：A 机器需要 `pip install pyinstaller`。

```cmd
cd D:\xtquant
tools\build_popup_launcher.bat
```

这会：
- 用 pyinstaller 把 `popup_launcher.py` 打成单文件 `dist\popup_launcher.exe`（含 pywebview 依赖）
- 在 A 机器 `HKCU\Software\Classes\xtquant-popup` 写入协议注册（**仅 A 当前用户**，无需管理员）

如果只想打包不注册：
```cmd
python tools\build_popup_launcher.py --no-register
```

如果只想重新注册协议（exe 已存在）：
```cmd
python tools\build_popup_launcher.py --register-only --exe-path "%CD%\dist\popup_launcher.exe"
```

### 4. 把 exe 拷贝到 B 机器

把 `dist\popup_launcher.exe` 拷到 B 机器任意目录（如 `D:\xtquant-popup\`）。

## B 机器（局域网电脑）配置

### 1. 安装 WebView2 Runtime

B 机器需要装 **Microsoft Edge WebView2 Runtime**（Windows 10/11 通常已自带；没装的话 pywebview 启动会提示下载）。
下载：https://developer.microsoft.com/microsoft-edge/webview2/

### 2. 注册协议（关键步骤！）

把 A 上 `dist\popup_launcher.exe` 拷贝到 B 机器任意目录（如 `D:\xtquant-popup\`）。

**⚠ 仅双击 exe 不会注册协议**——双击只是直接启动一个 PyWebView 窗口。注册协议必须显式执行：

```cmd
D:\xtquant-popup\popup_launcher.exe --register
```

预期输出：
```
[register] OK -> "D:\xtquant-popup\popup_launcher.exe" "%1"
            (written to HKCU, no admin required)
```

注销协议（如要清理）：
```cmd
D:\xtquant-popup\popup_launcher.exe --unregister
```

查看注册状态：
```cmd
D:\xtquant-popup\popup_launcher.exe --register-status
```

> 协议注册后，浏览器看到 `xtquant-popup://` 链接就会把控制权交给 `popup_launcher.exe`。

### 3. 浏览器访问 A 服务

B 机器浏览器打开 `http://A_IP:5000`（如 `http://192.168.1.10:5000`），点击任意『独立弹窗』按钮（如期权看板的「🖥 独立弹窗」、市场概况的「行业分布窗口」等）：
- **已注册协议** → B 桌面弹出 PyWebView 窗口（直接取 A 的数据）
- **没注册协议** → 浏览器 F12 会输出 `scheme does not have a registered handler`，前端立即降级在新标签页打开弹窗 URL（功能等价，但有浏览器外框）。Element-UI 顶部会显示「PyWebView 未注册协议，浏览器降级」提示

## 故障排查

| 现象 | 可能原因 | 解决 |
|------|----------|------|
| B 点按钮后**无任何反应** | 浏览器拦截了协议唤起 | 浏览器地址栏左侧查看是否被「屏蔽弹窗」；允许 |
| B 点按钮后**新标签打开**但**不是 PyWebView** | B 没装客户端/没注册协议 | 按上面 B 机器第 2 步注册 |
| 弹窗是 PyWebView 但**数据为空** | popup_launcher.py 里的 url 用了 127.0.0.1 | A 机器 config.py 设 `PUBLIC_HOST = "A 的局域网 IP"` |
| 弹窗一闪就关 | 依赖的 WebView2 Runtime 没装 | B 装 WebView2 Runtime |
| A 上点按钮**没反应** | launcher 子进程启动失败 | 看 `logs\popup_launcher.log`；常见原因：pywebview/pythonnet 与 Python 3.13 ABI 不匹配 |

## 协议 URL 格式

```
xtquant-popup://?url=<http://A:5000/option/popup>&title=<标题>&w=<宽>&h=<高>&minw=200&minh=200
```

- `url`：弹窗要加载的完整 URL（必填）
- `title`：窗口标题
- `w` / `h`：默认宽高（数字）
- `minw` / `minh`：最小宽高（可选，默认 200×200）

launcher 收到后会用 `urllib.parse` 解析 query，然后调 `webview.create_window(...)`。

## 架构图

```
┌─────────────── A 机器（数据源）────────────────┐         ┌─────── B 机器（局域网电脑）─────────┐
│                                                │         │                                     │
│  浏览器 ←── http://A:5000/option/board        │  HTTP   │  浏览器访问 http://A:5000/...        │
│   ↓ 点击独立弹窗                                │ ◄─────► │   ↓ 点击独立弹窗                    │
│   fetch POST /option/popup/launch              │         │   fetch POST /option/popup/launch   │
│   ↓                                            │         │   ↓                                │
│  Flask (waitress)                              │         │  Flask (A 上)                       │
│   ├─ 127.0.0.1 → Popen popup_launcher.py      │         │   ├─ REMOTE_ADDR=192.168.x.x        │
│   │   → A 桌面弹窗 ✅                          │         │   │   → 不 Popen                    │
│   └─ 192.168.x.x → 返回 client_launch          │         │   │   → 返回 {url, title, w, h}      │
│                     参数(url/title/w/h)        │         │   │                                │
│                                                │         │   ↓                                │
│                                                │         │  xtquant-popup://?url=...           │
│                                                │         │   ↓                                │
│                                                │         │  Windows 协议分发                   │
│                                                │         │   ↓                                │
│                                                │         │  popup_launcher.exe (B 本地)        │
│                                                │         │   ↓                                │
│                                                │         │  webview.create_window              │
│                                                │         │   ↓                                │
│                                                │         │  B 桌面 PyWebView 弹窗 ✅           │
│                                                │         │  (从 http://A:5000 取数据)          │
└────────────────────────────────────────────────┘         └─────────────────────────────────────┘
```
