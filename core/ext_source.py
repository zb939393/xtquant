# -*- coding: utf-8 -*-
"""扩展行情「取数源策略」：国元证券合肥 ×5 为唯一主源，故障时熔断切备用池。

## 为什么要收敛到单一主源

扩展行情池原有 32 台（长城/国元/国信/川财/中信/华泰/国君/通达信/银河）。多站随机选路
对**实时增量**无害（`vwap_service._merge_bars` 已冻结已收盘 bar），但对**历史深拉**有害：
实测同一根已收盘分钟 bar 在不同券商站之间存在高/低/量微差，且**深度也不同**
（2026-09-22 实测：国元合肥联通2/移动2 回溯至 2020-07-17，其余 3 台至 2020-07-20）。
历史页混用多站 → ATR 递归链 → dev_atr → dev_z 漂移 → 已发生的信号翻转
（2026-09-21「IFL9 下午腿 13:41↔13:02 跳变」即此成因）。故：

  - **主源** = 国元合肥 5 台（同券商、同机房、不同运营商：电信/联通×2/移动×2），
    延迟实测 78~187ms，期指 + 沪/深期权五通道全通，深历史 2020-07 级（最深池）。
  - **历史深拉锚点** = 5 台中固定一台（`ANCHOR`），因为 5 台之间深历史也有 1 个交易日级差异，
    历史页只认这一台，绝不跨站混用。
  - **备用池** = 其余 27 台，仅在主源熔断（连续失败）时启用，用于拉**增量**。

## 熔断 / 恢复

  - 主源连续失败达 `_FAIL_THRESHOLD` 轮 → 进入 failover（`_FAILOVER_SEC` 秒），
    此间 `active_pairs()` 返回备用池；失败轮次按「一次取数调用整体失败」计，不是按台计。
  - failover 到期自动回到主源（先试主源，成功即清零计数）。
  - 与 `tdx_pool_router` 分工：router 管「池内哪台快」，本模块管「用哪个池」。

刻意不 import pytdx / 重依赖，供 futures_service / option_exquote_service / vwap_service 共用。
"""
import os
import threading
import time

# ---------------------------------------------------------------- 主源：国元证券合肥 ×5
# 来源：D:/国元领航 客户端 connect.cfg [DSHOST] 段，逐台 TdxExHq_API 实测（2026-09-10 入库，
# 2026-09-22 复验：五通道全通、延迟 78~187ms、深历史最深）。
PRIMARY_SERVERS = [
    ("国元扩展行情合肥联通1", "220.248.233.5", 7721),
    ("国元扩展行情合肥移动1", "120.210.144.2", 7721),
    ("国元扩展行情合肥移动2", "221.130.121.45", 7721),
    ("国元扩展行情合肥联通2", "218.106.80.15", 7721),
    ("国元扩展行情合肥电信1", "61.191.48.15", 7721),
]

# 历史深拉锚点：5 台同源，但深度略有差异（联通2/移动2 至 2020-07-17，其余至 2020-07-20），
# 历史页必须只认一台，否则跨站微差会污染 ATR 递归链。选联通1（入库最早、实测最深最稳）。
ANCHOR = ("220.248.233.5", 7721)

_PRIMARY_PAIRS = [(ip, port) for (_n, ip, port) in PRIMARY_SERVERS]


def primary_pairs():
    """主源 (ip, port) 列表（国元合肥 5 台）。"""
    return list(_PRIMARY_PAIRS)


def anchor_pair():
    """历史深拉锚点 (ip, port) —— 5 台中固定的一台，历史页只走它。"""
    return ANCHOR


def anchor_name():
    """锚点站名（写进回补/备份的 source 字段，便于事后追溯）。"""
    for name, ip, port in PRIMARY_SERVERS:
        if (ip, port) == ANCHOR:
            return name
    return "%s:%d" % ANCHOR


def backup_pairs():
    """备用池 (ip, port) 列表（除国元合肥 5 台外的全部可用站），仅熔断时启用。"""
    try:
        try:
            from core.tdx_ext_servers import _TDX_EXT_SERVERS
        except Exception:
            from tdx_ext_servers import _TDX_EXT_SERVERS
    except Exception:
        return []
    prim = set(_PRIMARY_PAIRS)
    out = []
    for (_n, ip, port) in (_TDX_EXT_SERVERS or []):
        if (ip, port) not in prim:
            out.append((ip, port))
    return out


# ---------------------------------------------------------------- 熔断状态
_FAIL_THRESHOLD = 3         # 主源连续失败轮数 → 熔断切备用
_FAILOVER_SEC = 120.0       # 备用态最短持续时长（秒），到期回主源
_MAX_EVENTS = 20            # 事件环形缓冲长度

_LOCK = threading.RLock()
_ST = {
    "fails": 0,             # 主源连续失败轮数
    "failover_until": 0.0,  # 备用态截止时间戳（0 = 主源态）
    "last_ok": 0.0,         # 最近一次主源成功时间
    "last_fail": 0.0,       # 最近一次主源失败时间
    "primary_rounds": 0,    # 主源态取数轮数
    "backup_rounds": 0,     # 备用态取数轮数
    "switch_count": 0,      # 累计熔断切换次数
    "events": [],           # [(ts, kind, detail), ...]
}


def _ev(kind, detail):
    _ST["events"].append({"ts": time.time(), "kind": kind, "detail": detail})
    if len(_ST["events"]) > _MAX_EVENTS:
        del _ST["events"][:-_MAX_EVENTS]


def mode():
    """当前取数模式：'primary'（国元合肥）| 'failover'（备用池）。"""
    with _LOCK:
        return "failover" if time.time() < _ST["failover_until"] else "primary"


def active_pairs():
    """本次取数应使用的 (ip, port) 候选列表。

    - primary 模式 → 主源 5 台（国元合肥）。
    - failover 模式 → 备用池 27 台；若备用池为空（异常）则退回主源，绝不返回空列表。
    """
    if mode() == "primary":
        _ST["primary_rounds"] += 1
        return primary_pairs()
    with _LOCK:
        _ST["backup_rounds"] += 1
    bp = backup_pairs()
    return bp if bp else primary_pairs()


def report_round(ok, used_primary=None):
    """上报「一轮取数」的整体成败（由 futures_service._get_api 调用）。

    ok=True  → 主源态下清零连续失败、解除熔断；备用态下不改变主源计数（等探测期自然回切）。
    ok=False → 主源态下累计失败，达阈值即熔断切备用。
    """
    now = time.time()
    if used_primary is None:
        used_primary = (mode() == "primary")
    with _LOCK:
        if ok:
            if used_primary:
                _ST["fails"] = 0
                _ST["last_ok"] = now
                if _ST["failover_until"] > now:
                    _ev("restore", "主源恢复，回到国元合肥主源")
                _ST["failover_until"] = 0.0
            return
        if not used_primary:
            return
        _ST["fails"] += 1
        _ST["last_fail"] = now
        if _ST["fails"] >= _FAIL_THRESHOLD and _ST["failover_until"] <= now:
            _ST["failover_until"] = now + _FAILOVER_SEC
            _ST["switch_count"] += 1
            _ev("failover", "主源连续失败 %d 轮 → 切备用池 %ds" % (_ST["fails"], int(_FAILOVER_SEC)))


def force_failover(seconds=_FAILOVER_SEC):
    """手动切备用池（运维用）。"""
    with _LOCK:
        _ST["failover_until"] = time.time() + float(seconds)
        _ST["switch_count"] += 1
        _ev("failover", "手动切备用池 %ds" % int(seconds))


def force_primary():
    """手动切回主源（运维用）。"""
    with _LOCK:
        _ST["failover_until"] = 0.0
        _ST["fails"] = 0
        _ev("restore", "手动切回国元合肥主源")


def state():
    """运维视图：当前模式、熔断计数、延迟排名、最近事件。"""
    now = time.time()
    with _LOCK:
        st = dict(_ST)
        st["events"] = list(_ST["events"])
    try:
        from core import tdx_pool_router as _router
    except Exception:
        try:
            import tdx_pool_router as _router
        except Exception:
            _router = None
    lat = {}
    if _router is not None:
        try:
            for it in _router.snapshot():
                lat[(it["ip"], it["port"])] = it
        except Exception:
            pass

    def _nodes(pairs):
        out = []
        for ip, port in pairs:
            it = lat.get((ip, port)) or {}
            out.append({
                "ip": ip, "port": port,
                "ema_ms": it.get("ema_ms"),
                "ok": it.get("ok", 0), "fail": it.get("fail", 0),
                "cooldown_left": it.get("cooldown_left", 0.0),
            })
        return out

    return {
        "mode": "failover" if now < st["failover_until"] else "primary",
        "failover_left": max(0.0, round(st["failover_until"] - now, 1)),
        "primary_fails": st["fails"],
        "primary_rounds": st["primary_rounds"],
        "backup_rounds": st["backup_rounds"],
        "switch_count": st["switch_count"],
        "last_primary_ok": st["last_ok"],
        "last_primary_fail": st["last_fail"],
        "anchor": {"name": anchor_name(), "ip": ANCHOR[0], "port": ANCHOR[1]},
        "primary": _nodes(primary_pairs()),
        "backup_count": len(backup_pairs()),
        "events": st["events"][-10:],
    }


def snapshot():
    """兼容 tdx_pool_router.snapshot() 风格的别名（运维接口用）。"""
    return state()


def reset():
    """清空熔断状态（仅供测试）。"""
    with _LOCK:
        _ST.update({
            "fails": 0, "failover_until": 0.0, "last_ok": 0.0, "last_fail": 0.0,
            "primary_rounds": 0, "backup_rounds": 0, "switch_count": 0, "events": [],
        })
