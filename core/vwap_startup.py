# -*- coding: utf-8 -*-
"""VWAP 深历史启动自愈 / 运维触发：保证 data/vwap_hist/<code>_1min.pkl 完整。

统一委托给 core.vwap_service._ensure_deep_history（单一编排入口，自带刷新锁，避免与
dev_z 计算路径自触发回补相互打架、写同一文件）。两者语义完全一致：

  · 先从国元合肥锚点拉取深历史（reuse backfill：resume + union 取最长 + 填补缺口）；
  · 与本地历史存档（vwap_hist / 备份）比对，并集合并（旧值优先、已收盘 bar 冻结）；
  · 写回 data/vwap_hist（形成磁盘缓存），并刷新备份（天数只增不减）；
  · 回填进程内 _FULL_HIST 缓存，使 dev_z 不必重启即切换到深历史。

对齐需求（2026-09-24）：「计算 dev_z 先从国元服务器调用深历史数据，比较历史存档，
取最长数据，或填补，并形成缓存」。
"""
import logging

CODES = ["IFL9", "IHL9", "ICL9", "IML9"]


def _log():
    return logging.getLogger("vwap-bootstrap")


def ensure_vwap_history(codes=None, min_days=277, background=True):
    """确保 VWAP 深历史完整（非阻塞）。

    委托 core.vwap_service._ensure_deep_history：各品种在后台并发回补（实际并发由
    vwap_service 的每品种守护线程承担），立即返回，不阻塞调用方。同一品种不会重复回补；
    不同品种并发执行。

    codes：待确保的品种；min_days：视为「齐备」的最小交易日数（对齐 baseline_min_days=277）。
    """
    codes = list(codes) if codes else list(CODES)
    try:
        try:
            from core import vwap_service as _vs
        except Exception:
            import vwap_service as _vs
    except Exception as e:
        _log().error("[vwap-bootstrap] 无法 import vwap_service，启动恢复中止：%r", e)
        return
    for code in codes:
        try:
            _vs._ensure_deep_history(code, min_days)
        except Exception as e:
            _log().exception("[vwap-bootstrap] %s 触发深度回补失败：%r", code, e)
