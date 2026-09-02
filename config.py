# -*- coding: utf-8 -*-
"""项目配置：所有可调参数集中在这里，业务代码只读 Config。"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Config:
    # ============== Flask ==============
    HOST = "0.0.0.0"
    PORT = 5000
    DEBUG = True
    SECRET_KEY = "change-me-in-prod"

    # ============== 局域网弹窗 ==============
    # A 机器的局域网 IP / 域名（供 B 机器弹窗访问的 URL 前缀使用）。
    # 推荐填写，例如 PUBLIC_HOST = "192.168.1.10"。
    # 留空则使用请求 host 头推断（B 访问时使用的 IP/域名）作为兜底。
    PUBLIC_HOST = ""
    # 除 127.0.0.1/::1 外，仍按『服务器本机』处理的 IP 列表（罕见用法）。
    # 例如 ["192.168.1.10"] 表示 A 用局域网 IP 访问自己的服务时仍走本机启动逻辑。
    LAN_LOCAL_IPS = []

    # ============== xtquant（miniQMT 桥）==============
    # miniQMT / iQuant 的 userdata 目录
    XT_PATH = r"D:\国信iQuant策略交易平台\userdata_mini"
    XT_SESSION_ID = 123456
    XT_ACCOUNT_ID = "YOUR_ACCOUNT_ID"      # 模拟账号填这里
    XT_ACCOUNT_TYPE = "STOCK"              # STOCK / CREDIT / FUTURE / OPTION

    # ============== HTTP 桥（路径 C，无 miniQMT 时用）==============
    BRIDGE_URL   = "http://127.0.0.1:58080"
    BRIDGE_TOKEN = "change-me-to-your-token"

    # 实时行情桥 /cb_rt_snapshot 客户端超时(秒)。全量 universe(约320转债+320正股)
    # 经 iQuant get_market_data 实测 ~6.3s, 冷启动更久; 8s 默认值会在冷启动/并发排队时
    # 误触发 PyTDX 兜底, 故上调到 20, 与 akshare 侧 timeout=20 对齐。
    CB_RT_BRIDGE_TIMEOUT = 20
    # 交易后端选择: "xtquant"(直接连miniQMT) / "bridge"(走HTTP桥) / "dry"(只打日志)
    TRADE_BACKEND = "dry"  # dry(开箱即用) / bridge(走HTTP桥,需iQuant运行桥服务) / xtquant(需miniQMT)

    # ============== akshare ==============
    AK_CACHE_TTL = 300  # 秒

    # ============== 期权监控模块 ==============
    # 合约列表（静态信息：标的/行权价/到期日/类型）缓存 TTL（秒）。
    # 期权合约日内不会变动（仅在每月 rollover 时调整），采用「日内长缓存 + 每日盘前失效」：
    # 此处 TTL 仅作为磁盘缓存不被判定「过期」的兜底（须 ≥ 一个自然日），真正的「跨天自动重抓」
    # 由 core/option_service.option_contracts 内的日期闸门控制（自然日变化即失效）。
    OPTION_STATIC_TTL = 86400
    # 实时行情 + 指标计算 缓存 TTL（秒）—— 前端 3~5s 轮询，10s 刷新足够平滑
    OPTION_RT_TTL = 10
    # Black-Scholes 无风险利率（年化）
    OPTION_RISK_FREE_RATE = 0.02
    # Black-Scholes 默认波动率（当年化隐波无法反解时回退，例如深度实值无时间价值）
    OPTION_VOL_DEFAULT = 0.25

    # ============== PyTDX 实时行情抓取 ==============
    # 并行抓取线程上限（线程级连接池：每线程一个持久连接）
    RT_WORKERS = 48
    # PyTDX 阻塞型行情请求的并发上限（信号量）。看盘页多行同时轮询会并发发起大量
    # get_security_bars / get_security_quotes 等阻塞调用，超出服务端承受力会触发
    # ERR_CONNECTION_REFUSED。用此上限把并发的 PyTDX 网络调用收敛到固定值，其余排队，
    # 从后端侧给行情请求做节流。缓存命中（TTL 内）不占用此配额。
    PYTDX_MAX_CONCURRENCY = 8
    # 每连接分片大小：越小并行度越高（受服务器侧单连接稳定性约束，不宜过小）
    RT_CHUNK = 14
    # 服务器延迟优选：启动期测速排序，仅从前 N 台最快服务器里随机选，消除冷启动卡顿
    RT_SERVER_TOP_N = 4

    # ============== 路径 ==============
    LOG_DIR  = os.path.join(BASE_DIR, "logs")
    DATA_DIR = os.path.join(BASE_DIR, "data")

    @classmethod
    def ensure_dirs(cls):
        for d in (cls.LOG_DIR, cls.DATA_DIR):
            os.makedirs(d, exist_ok=True)
