# -*- coding: utf-8 -*-
"""独立弹窗默认尺寸配置。

 Centralized configuration for the default width/height of every independent
 popup window.  The frontend ``popup-launcher.js`` reads these dimensions from
 the backend ``popup_launch`` JSON response; the integrated popup uses the same
 table to set an appropriate container size.

 更新窗口默认尺寸 → 仅改这里即可，所有 launch 路由 + integrated 弹窗自动生效。
"""
# 每个独立弹窗的默认尺寸（像素） + 最小尺寸。键名与 popup-launcher / futures_bp 路由语义对应。
# 宽度 / 高度 / 最小宽度 / 最小高度 —— 用于 PyWebView 窗口创建以及协议唤起降级。
POPUP_SIZES = {
    # 股指期货综合看盘
    "futures":           {"width": 1240, "height": 700, "min_w": 900, "min_h": 400},
    # 7×24 快讯
    "news":              {"width": 410,  "height": 380, "min_w": 50,  "min_h": 50},
    # 股指资金
    "capital":           {"width": 320,  "height": 400, "min_w": 50,  "min_h": 50},
    # 行业板块分布
    "industry":          {"width": 400,  "height": 400, "min_w": 50,  "min_h": 50},
    # 板块成分股分布
    "industry_stocks":   {"width": 400,  "height": 400, "min_w": 50,  "min_h": 50},
    # 个股分时/K线
    "stock_chart":       {"width": 760,  "height": 520, "min_w": 50,  "min_h": 50},
    # 全市场涨幅分布
    "zhangfu":           {"width": 320,  "height": 180, "min_w": 50,  "min_h": 50},
    # 两市成交分析
    "amountflow":        {"width": 320,  "height": 500, "min_w": 50,  "min_h": 50},
    # 外围股市
    "external":          {"width": 400,  "height": 600, "min_w": 50,  "min_h": 50},
    # 期权自选股看盘
    "option":            {"width": 830,  "height": 380, "min_w": 200, "min_h": 200},
    # 综合一体弹窗 (集成所有子窗口)
    # 三列布局：左(320) + 中(400) + 右(1240) = 1960, 间隙+内边距 ~40 → 2000
    # 列高：左=max(500,180,400)=1080 / 中=max(400,600)=1000 / 右=max(700,380)=1080
    # 总高 = 40(toolbar) + 8(pad) + 1080 + 8(pad) = 1136 ≈ 1140
    "integrated":        {"width": 2000, "height": 1140, "min_w": 1200, "min_h": 700},
}

# 集成弹窗内部各标签页对应的 iframe 加载路径。
# key 与 POPUP_SIZES 保持一致，便于统一索引。
POPUP_ROUTES = {
    "futures":        "/futures/popup",
    "news":           "/futures/news/popup",
    "capital":        "/futures/capital/popup",
    "industry":       "/futures/industry/popup",
    "zhangfu":        "/futures/zhangfu/popup",
    "amountflow":     "/futures/amountflow/popup",
    "external":       "/futures/external/popup",
    "option":         "/option/popup",
}

# 集成弹窗 Tab 标签文字（中文）。
POPUP_TITLES = {
    "futures":        "期指综合看盘",
    "news":           "7×24 快讯",
    "capital":        "股指资金",
    "industry":       "行业分布",
    "zhangfu":        "涨幅分布",
    "amountflow":     "两市成交分析",
    "external":       "外围股市",
    "option":         "期权自选",
}

# 集成弹窗中各 Tab 的默认高度比例（相对于弹窗可用高度）。
# 这些值只是初始分配，resize 时会等比缩放。
POPUP_HEIGHT_RATIOS = {
    "futures":        0.55,
    "news":           0.20,
    "capital":        0.20,
    "industry":       0.40,
    "zhangfu":        0.25,
    "amountflow":     0.30,
    "external":       0.45,
    "option":         0.50,
}


def get_popup_size(key, fallback_key="integrated"):
    """获取某个弹窗的默认尺寸。

    参数:
        key: POPUP_SIZES 的键名。
        fallback_key: 当 key 不存在时回退的键。

    返回: dict(width, height, min_w, min_h)
    """
    return POPUP_SIZES.get(key, POPUP_SIZES.get(fallback_key, POPUP_SIZES["integrated"]))
