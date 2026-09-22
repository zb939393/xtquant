# -*- coding: utf-8 -*-
"""VWAP 1 分钟历史的**备份 / 校验 / 恢复 / 灾备增量**。

## 要解决的问题

`data/vwap_hist/<code>_1min.pkl` 是 expanding 基线（dev_z 的 μ/σ）的唯一数据源，
由 `tools/backfill_vwap_history.py` 从国元合肥主站回补 277+ 交易日。它有两个脆弱点：

  1. **文件可能被误删 / 被半截写入 / 被低质量源覆盖** —— 一旦没了，基线退化为
     实时缓存口径（30 日），下午腿信号口径立刻改变。
  2. **主源（国元合肥 5 台）整体不可用时**，需要靠备份先把历史接住，再用备用池拉增量。

## 备份口径：天数只增不减（「最大数据天数」）

每次备份都用**并集合并**（date 为键、旧值优先）：

    backup_new = union(backup_old, source_now)

  - 旧备份里已有的 bar **一律保留原值**（历史冻结，绝不被新源改写 —— 这是
    `_merge_bars` 同一条铁律，避免多站微差污染 ATR 递归链）；
  - 源里新出现的日期**追加**进来（天数只增不减）；
  - 记录 `max_days`（历史最大观测天数），单调递增，即使源退化了备份也不会缩水。

备份时**只收已完整收盘的交易日**（bar 数 ≥ `_MIN_BARS_DAY`，且当天未到 15:05 不算完），
避免把一个还在形成中的交易日冻结成残缺日。

## 三类落盘

  - `data/vwap_backup/<code>_max.pkl`   —— 最大天数主备份（**唯一恢复源**）
  - `data/vwap_backup/snapshots/<code>_<ts>.pkl` —— 时间戳快照，仅当天数变化时写，
    滚动保留最近 `MAX_SNAPSHOTS` 份
  - `data/vwap_backup/manifest.json`    —— 清单（每品种天数/根数/区间/最大天数/时间/来源）

## 灾备流程（主源挂掉时）

    restore(code)  →  备份写回 data/vwap_hist（历史接住）
    heal(code, fetch_inc)  →  再用**备用池**拉增量补到最新，合并落盘 + 更新备份

`heal` 的增量取数函数由调用方注入（`tools/vwap_backup.py` 里用备用池实现），
本模块刻意不 import pytdx，保证可在纯离线环境做校验/恢复。
"""
import os
import json
import time
import pickle
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST_DIR = os.path.join(_ROOT, "data", "vwap_hist")
CACHE_DIR = os.path.join(_ROOT, "data", "vwap_cache")
# 备份目录可用环境变量 XTQUANT_VWAP_BACKUP_DIR 指到**另一块盘**（灾难隔离：主盘挂了备份还在）。
# 默认 data/vwap_backup。
BACKUP_DIR = os.environ.get("XTQUANT_VWAP_BACKUP_DIR") or os.path.join(_ROOT, "data", "vwap_backup")
SNAP_DIR = os.path.join(BACKUP_DIR, "snapshots")
MANIFEST = os.path.join(BACKUP_DIR, "manifest.json")

CODES = ["IFL9", "IHL9", "ICL9", "IML9"]

MAX_SNAPSHOTS = 5        # 每品种保留的时间戳快照份数
_MIN_BARS_DAY = 200      # 一个交易日至少这么多根才算「完整收盘」（正常 240）
_EOD_HHMM = "15:05"      # 当日过了这个时刻，今天的 bar 才算走完
_LOCK = threading.RLock()


# ---------------------------------------------------------------- 基础 IO
def _rel_or_abs(path, base):
    """相对项目根的显示路径；跨盘符（备份放到别的盘）时退回绝对路径，绝不抛异常。"""
    try:
        return os.path.relpath(path, base)
    except Exception:
        return os.path.abspath(path)


def _safe_name(code):
    return str(code).replace("/", "_").replace("\\", "_")


def hist_path(code):
    return os.path.join(HIST_DIR, "%s_1min.pkl" % _safe_name(code))


def cache_path(code):
    return os.path.join(CACHE_DIR, "%s_1min.pkl" % _safe_name(code))


def backup_path(code):
    return os.path.join(BACKUP_DIR, "%s_max.pkl" % _safe_name(code))


def _read_bars(path):
    """读 pkl 取 bars 列表；损坏/缺失返回 []。"""
    try:
        with open(path, "rb") as f:
            obj = pickle.load(f)
    except Exception:
        return []
    if isinstance(obj, dict):
        b = obj.get("bars")
        return b if isinstance(b, list) else []
    return obj if isinstance(obj, list) else []


def _read_obj(path):
    try:
        with open(path, "rb") as f:
            obj = pickle.load(f)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _write_pkl_atomic(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(obj, f, protocol=4)
    os.replace(tmp, path)


# ---------------------------------------------------------------- bar 工具
def _day_of(bar):
    return str(bar.get("date") or "")[:10]


def _hm_of(bar):
    s = str(bar.get("date") or "")
    return s[11:16] if len(s) >= 16 else ""


def _now_hhmm():
    return time.strftime("%H:%M")


def is_complete_day(day, n_bars, now_day):
    """交易日是否已完整收盘：bar 数够，且（不是今天，或今天已过 15:05）。"""
    if n_bars < _MIN_BARS_DAY:
        return False
    if day == now_day and _now_hhmm() < _EOD_HHMM:
        return False
    return True


def complete_days(bars, now_day=None):
    """返回已完整收盘的交易日集合。"""
    if now_day is None:
        now_day = time.strftime("%Y-%m-%d")
    cnt = {}
    for b in bars:
        d = _day_of(b)
        if d:
            cnt[d] = cnt.get(d, 0) + 1
    return set(d for d, n in cnt.items() if is_complete_day(d, n, now_day))


def union_bars(old, new):
    """并集合并：date 为键，**旧值优先**（历史冻结），新日期追加。返回 (merged, added)。

    与 vwap_service._merge_bars 同一条铁律：已收盘的 bar 一经落盘即冻结，
    绝不让不同服务器站的历史页微差改写它（否则 ATR→dev_z 会漂移、信号翻转）。
    """
    idx = {}
    for b in (old or []):
        d = str(b.get("date") or "")
        if d:
            idx[d] = b
    added = 0
    for b in (new or []):
        d = str(b.get("date") or "")
        if d and d not in idx:
            idx[d] = b
            added += 1
    merged = sorted(idx.values(), key=lambda x: str(x.get("date") or ""))
    return merged, added


def bar_stats(bars):
    """统计根数 / 交易日数 / 区间 / 残缺交易日。"""
    days = {}
    for b in bars:
        d = _day_of(b)
        if d:
            days[d] = days.get(d, 0) + 1
    ds = sorted(days)
    short = [d for d in ds if days[d] < _MIN_BARS_DAY]
    return {
        "n": len(bars),
        "days": len(ds),
        "first": (bars[0].get("date") if bars else None),
        "last": (bars[-1].get("date") if bars else None),
        "first_day": (ds[0] if ds else None),
        "last_day": (ds[-1] if ds else None),
        "short_days": short[:10],
        "short_count": len(short),
        "avg_bars_per_day": round(len(bars) / float(len(ds)), 1) if ds else 0.0,
    }


# ---------------------------------------------------------------- manifest
def _load_manifest():
    try:
        with open(MANIFEST, "r", encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _save_manifest(m):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    tmp = MANIFEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1)
    os.replace(tmp, MANIFEST)


def manifest():
    """返回备份清单（含每个品种的最大天数记录）。"""
    return _load_manifest()


# ---------------------------------------------------------------- 快照轮转
def _rotate_snapshots(code, keep=MAX_SNAPSHOTS):
    try:
        pre = _safe_name(code) + "_"
        files = [f for f in os.listdir(SNAP_DIR) if f.startswith(pre) and f.endswith(".pkl")]
    except Exception:
        return
    files.sort()
    for f in files[:-keep]:
        try:
            os.remove(os.path.join(SNAP_DIR, f))
        except Exception:
            pass


# ---------------------------------------------------------------- 备份
def backup_code(code, source="", snapshot=None, hist=None, cache=None):
    """备份单个品种：并集合并（天数只增不减）→ 写主备份 + manifest（+ 按需快照）。

    参数 hist / cache 可显式传入 bars（测试用）；为 None 时读磁盘。
    返回统计 dict（含 before/after 天数、max_days、snapshot 是否写出）。
    """
    code = str(code)
    with _LOCK:
        old_obj = _read_obj(backup_path(code))
        old_bars = old_obj.get("bars") or []
        old_stats = bar_stats(old_bars)
        old_max_days = int(old_obj.get("max_days") or old_stats["days"] or 0)

        hist_bars = list(hist) if hist is not None else _read_bars(hist_path(code))
        cache_bars = list(cache) if cache is not None else _read_bars(cache_path(code))
        src = list(hist_bars)
        src, _ = union_bars(src, cache_bars)

        # 只收已完整收盘的交易日，避免冻结残缺日
        ok_days = complete_days(src)
        src = [b for b in src if _day_of(b) in ok_days]

        merged, added = union_bars(old_bars, src)
        st = bar_stats(merged)
        max_days = max(old_max_days, st["days"])
        src_days = len(set(_day_of(b) for b in src if _day_of(b)))

        obj = {
            "bars": merged,
            "ts": time.time(),
            "code": code,
            "source": source or "",
            "days": st["days"],
            "max_days": max_days,
            "n": st["n"],
            "first_day": st["first_day"],
            "last_day": st["last_day"],
        }
        _write_pkl_atomic(backup_path(code), obj)

        wrote_snap = False
        if snapshot is None:
            snapshot = st["days"] > old_stats["days"]      # 天数长进了才留快照
        if snapshot and merged:
            os.makedirs(SNAP_DIR, exist_ok=True)
            p = os.path.join(SNAP_DIR, "%s_%s.pkl" % (_safe_name(code),
                                                      time.strftime("%Y%m%d_%H%M%S")))
            try:
                _write_pkl_atomic(p, obj)
                wrote_snap = True
                _rotate_snapshots(code)
            except Exception:
                wrote_snap = False

        m = _load_manifest()
        m[code] = {
            "days": st["days"], "n": st["n"], "max_days": max_days,
            "first_day": st["first_day"], "last_day": st["last_day"],
            "ts": obj["ts"], "source": obj["source"],
            "short_count": st["short_count"],
            "backup_file": _rel_or_abs(backup_path(code), _ROOT),
        }
        _save_manifest(m)

        return {
            "code": code, "days_before": old_stats["days"], "days": st["days"],
            "src_days": src_days, "added_bars": added, "n": st["n"], "max_days": max_days,
            # 源比备份还少 → 本次完全靠旧备份兜住（数据源退化的信号，需报警）
            "kept_max": bool(max_days > src_days),
            "first_day": st["first_day"], "last_day": st["last_day"],
            "short_count": st["short_count"], "snapshot": wrote_snap,
        }


def backup_all(codes=None, source=""):
    """备份全部（默认四品种），返回 {code: stats}。"""
    out = {}
    for c in (codes or CODES):
        try:
            out[c] = backup_code(c, source=source)
        except Exception as e:
            out[c] = {"code": c, "error": repr(e)}
    return out


# ---------------------------------------------------------------- 校验
def verify(code, deep=False):
    """校验备份与当前历史的一致性（只读）。

    返回 dict：备份/历史各自的根数、天数、区间；
      - missing_days：**完整交易日**里历史有、备份没有的天数（备份落后 → 建议 backup）
      - extra_days  ：备份有、历史没有的交易日数（历史被删/被截断 → 建议 restore）
      - hist_incomplete_days：历史里的残缺交易日（bar 数 < 200，源本身不全，备份刻意不收）
      - ok          ：备份覆盖历史的全部完整交易日，且备份内无残缺日
    """
    code = str(code)
    bk_bars = _read_bars(backup_path(code))
    hs_bars = _read_bars(hist_path(code))
    bk = bar_stats(bk_bars)
    hs = bar_stats(hs_bars)

    def _days(bars):
        return set(_day_of(b) for b in bars if _day_of(b))

    bd, hd = _days(bk_bars), _days(hs_bars)
    hs_complete = complete_days(hs_bars)      # 历史里已完整收盘的交易日
    # 备份须覆盖历史的全部**完整**交易日；历史里的残缺日（源本身不全）不计入缺口
    missing = sorted(hs_complete - bd)
    extra = sorted(bd - hd)
    incomplete = sorted(hd - hs_complete)
    ok = bool(bk["n"] > 0 and bk["short_count"] == 0 and not missing)
    if deep and ok:
        ok = (len(missing) == 0)
    res = {
        "code": code,
        "backup": bk, "hist": hs,
        "backup_exists": os.path.exists(backup_path(code)),
        "hist_exists": os.path.exists(hist_path(code)),
        "missing_days": len(missing), "missing_sample": missing[:5],
        "extra_days": len(extra), "extra_sample": extra[:5],
        "hist_incomplete_days": len(incomplete), "incomplete_sample": incomplete[:5],
        "ok": ok,
    }
    if not res["ok"] and extra:
        res["advice"] = "历史比备份短 %d 天 → 建议 restore()" % len(extra)
    elif not res["ok"] and missing:
        res["advice"] = "备份落后历史 %d 天 → 建议 backup()" % len(missing)
    return res


# ---------------------------------------------------------------- 恢复
def restore(code, dry_run=False):
    """把最大天数备份写回 data/vwap_hist/<code>_1min.pkl（灾备恢复）。

    写回内容是 **union(备份, 现有历史)** —— 合并而非覆盖，保证「历史只增不减」：
    备份缺失的当天残缺 bar 也保留，绝不因为恢复而丢掉任何一天。
    原文件先另存为 .bak_<ts>，然后原子替换。dry_run=True 只报告不落盘。
    """
    code = str(code)
    with _LOCK:
        src = backup_path(code)
        if not os.path.exists(src):
            return {"code": code, "ok": False, "reason": "备份不存在: %s" % src}
        bk_bars = _read_bars(src)
        if not bk_bars:
            return {"code": code, "ok": False, "reason": "备份为空"}
        dst = hist_path(code)
        hs_bars = _read_bars(dst)
        merged, added = union_bars(bk_bars, hs_bars)   # 备份为主，现有历史补缺
        st = bar_stats(merged)
        cur = bar_stats(hs_bars)
        if dry_run:
            return {"code": code, "ok": True, "dry_run": True,
                    "would_write": st, "current": cur, "dst": dst}
        bkp = None
        if os.path.exists(dst):
            bkp = "%s.bak_%s" % (dst, time.strftime("%Y%m%d_%H%M%S"))
            try:
                import shutil
                shutil.copy2(dst, bkp)
            except Exception:
                bkp = None
        _write_pkl_atomic(dst, {"bars": merged, "ts": time.time(),
                                "source": "restore-from-backup", "code": code})
        return {"code": code, "ok": True, "written": st, "replaced": cur,
                "added_from_backup": added, "hist_bak": bkp, "dst": dst}


def restore_all(codes=None, dry_run=False):
    return {c: restore(c, dry_run=dry_run) for c in (codes or CODES)}


# ---------------------------------------------------------------- 灾备增量
def heal(code, fetch_inc, source="", max_rounds=8):
    """主源不可用时的**灾备愈合**：备份接住历史 → 备用池拉增量 → 合并落盘 + 更新备份。

    fetch_inc(start, count) -> bars 列表（由调用方用备用池实现，start 为自今往回的偏移）。
    流程：
      1) 读最大天数备份（没有则读现有 hist）；
      2) 用 fetch_inc 拉最新若干页，union 进历史（旧值优先，历史冻结）；
      3) 写回 data/vwap_hist + 更新备份；全程不改写已收盘的历史 bar。
    返回 dict（含 baseline_days 前后对比、新增根数、来源标签）。
    """
    code = str(code)
    with _LOCK:
        bk_bars = _read_bars(backup_path(code))
        hs_bars = _read_bars(hist_path(code))
        base, _ = union_bars(bk_bars, hs_bars)      # 备份为主，历史补缺
        before = bar_stats(base)

        added = 0
        rounds = 0
        offset = 0
        for _ in range(max(1, int(max_rounds))):
            rounds += 1
            try:
                page = fetch_inc(offset, 700)
            except Exception:
                break
            if not page:
                break
            merged, add = union_bars(base, page)
            base = merged
            added += add
            if len(page) < 700:
                break                               # 源页不满 → 到底了
            if add == 0:
                break                               # 本页全是已有数据 → 已追平
            offset += 700                           # 继续往历史深处翻，填补空隙
        # 落盘：历史（完整收盘日过滤，去掉可能残缺的当日）
        ok_days = complete_days(base)
        clean = [b for b in base if _day_of(b) in ok_days]
        if clean:
            _write_pkl_atomic(hist_path(code), {"bars": clean, "ts": time.time(),
                                                "source": source or "heal",
                                                "code": code})
        after = bar_stats(clean if clean else base)
        res = {"code": code, "rounds": rounds, "added_bars": added,
               "days_before": before["days"], "days_after": after["days"],
               "last_day_before": before["last_day"], "last_day_after": after["last_day"],
               "source": source or "heal"}
        # 备份同步刷新（并集，天数只增不减）
        try:
            b = backup_code(code, source="heal:%s" % (source or ""), hist=clean or base,
                            snapshot=True)
            res["backup"] = {"days": b["days"], "max_days": b["max_days"]}
        except Exception as e:
            res["backup_error"] = repr(e)
        return res
