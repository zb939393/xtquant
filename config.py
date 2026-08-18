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

    # ============== PyTDX 实时行情抓取 ==============
    # 并行抓取线程上限（线程级连接池：每线程一个持久连接）
    RT_WORKERS = 48
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
