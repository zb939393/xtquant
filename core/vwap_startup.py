# -*- coding: utf-8 -*-
"""VWAP 深历史启动自愈：保证 data/vwap_hist/<code>_1min.pkl 完整。

对齐需求（2026-09-24）：「服务器启动时，先拉取国元服务器的数据，比较保存的历史数据，
根据实际情况处理，保证数据完整性」。

dev_z 基线（μ/σ by pos）唯一的设计深历史源是 data/vwap_hist/<code>_1min.pkl（应回补到
2020-05，约 1536 交易日）。它一旦缺失，VWAP 会静默退化成 30 日固定窗口。本模块在服务器
启动时把缺失的深历史补齐，且全程保证本地已存数据不被覆盖（union、旧值优先、已收盘 bar 冻结）。

每个品种的处理（对齐需求里的「比对 + 按实际情况处理」）：
  1) 检查 data/vwap_hist 是否齐备（交易日数 ≥ min_days 且非空）。齐备 → 跳过（不每次启动重拉）。
  2) 不齐备时：
     a. 先把本地已存数据接回 hist：若 hist 缺失但有备份 → restore（备份→hist，union）；
        若 hist 存在但不齐 → 用备份补其缺失日（union，旧值优先）。这一步保证**绝不丢本地数据**。
     b. 从国元合肥锚点回补缺口（复用 tools/backfill_vwap_history，单锚点、union、旧值优先、
        结束自动刷新备份）。国元是权威深源，补齐 2020→今的整段历史。
  3) 回补结束自动刷新备份（天数只增不减）。

全程后台守护线程执行，不阻塞 Flask 启动；pytdx / backfill 缺失则静默跳过（不崩服务）。
"""
import os
import sys
import time
import threading
import logging

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODES = ["IFL9", "IHL9", "ICL9", "IML9"]


def _log():
    return logging.getLogger("vwap-bootstrap")


def ensure_vwap_history(codes=None, min_days=277, background=True):
    """启动确保：补齐 VWAP 深历史。

    background=True（默认）：后台守护线程执行，立即返回线程对象，不阻塞调用方。
    codes：待确保的品种列表；min_days：视为「齐备」的最小交易日数（对齐 baseline_min_days=277）。
    """
    codes = list(codes) if codes else list(CODES)
    if background:
        t = threading.Thread(target=_ensure_all, args=(codes, min_days),
                             name="vwap-bootstrap", daemon=True)
        t.start()
        return t
    return _ensure_all(codes, min_days)


def _ensure_all(codes, min_days):
    try:
        from core import vwap_backup as vb
    except Exception:
        try:
            import vwap_backup as vb
        except Exception:
            _log().error("[vwap-bootstrap] 无法 import vwap_backup，启动恢复中止")
            return
    # 品种间并发：各 backfill 主要等待网络 IO（受 GIL 影响极小），4 品种从串行 4x 降到 1x
    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(
            max_workers=min(len(codes), 4),
            thread_name_prefix="vwap-bootstrap") as ex:
        list(ex.map(lambda c: _ensure_one(vb, c, min_days), codes))


def _ensure_one(vb, code, min_days):
    log = _log()
    hist_path = vb.hist_path(code)
    hist_bars = vb._read_bars(hist_path)
    st = vb.bar_stats(hist_bars)

    # 1) 已齐备 → 跳过（仅顺手刷新备份，保证 manifest 口径与 hist 一致）
    if st["days"] >= min_days and hist_bars:
        log.info("[vwap-bootstrap] %s 深历史已齐备（%d 日），跳过启动回补", code, st["days"])
        try:
            vb.backup_code(code, source="startup:skip", snapshot=False)
        except Exception:
            pass
        return

    log.warning("[vwap-bootstrap] %s 深历史不齐备（%d 日 < %d），启动恢复…",
                code, st["days"], min_days)

    # 2a) 先确保本地已存数据在 hist 中（对比保存的历史 → 按实际情况处理 → 保证完整性）
    bk_bars = vb._read_bars(vb.backup_path(code))
    if bk_bars:
        if not hist_bars:
            # hist 完全缺失：把备份接回 hist（union，历史冻结，绝不丢本地数据）
            try:
                r = vb.restore(code)
                written = (r.get("written") or {}).get("days")
                log.info("[vwap-bootstrap] %s restore 备份接回 hist：%s 日", code, written)
            except Exception as e:
                log.exception("[vwap-bootstrap] %s restore 失败：%r", code, e)
        else:
            # hist 存在但不齐：用备份补其缺失日（union，旧值优先）
            merged, added = vb.union_bars(hist_bars, bk_bars)
            if added:
                vb._write_pkl_atomic(hist_path, {
                    "bars": merged, "ts": time.time(),
                    "source": "startup:merge-backup", "code": code,
                })
                log.info("[vwap-bootstrap] %s 用备份补 hist 缺失 %d 根", code, added)

    # 2b) 从国元合肥锚点回补缺口（复用 backfill：单锚点、union、旧值优先、结束刷新备份）
    try:
        if os.path.join(_ROOT, "tools") not in sys.path:
            sys.path.insert(0, os.path.join(_ROOT, "tools"))
        from tools import backfill_vwap_history as bf
    except Exception:
        try:
            import backfill_vwap_history as bf
        except Exception as e:
            log.error("[vwap-bootstrap] %s 无法 import backfill（pytdx 未装？）：%r", code, e)
            return
    try:
        bf.backfill(code)   # 内部从现有 hist resume，只补缺失日；结束自动刷新备份
        log.info("[vwap-bootstrap] %s 国元锚点回补完成", code)
    except Exception as e:
        log.exception("[vwap-bootstrap] %s 国元回补异常（保留现有数据，不覆盖）：%r", code, e)
