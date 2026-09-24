# -*- coding: utf-8 -*-
"""VWAP 历史备份运维 CLI：备份 / 校验 / 恢复 / 灾备增量。

配套模块：core/vwap_backup.py（备份与恢复逻辑）、core/ext_source.py（主源策略）。

用法：
  python tools/vwap_backup.py backup            # 备份四品种（并集合并，天数只增不减）
  python tools/vwap_backup.py verify            # 校验备份 vs 历史（只读）
  python tools/vwap_backup.py restore --yes     # 用备份恢复 data/vwap_hist（灾备）
  python tools/vwap_backup.py heal              # 灾备：备份接住历史 + 备用池拉增量
  python tools/vwap_backup.py status            # 取数源 + 备份清单

约定：
  - 备份永不改写已收盘的历史 bar（旧值优先），天数单调不减（max_days 只增）。
  - `heal` 默认用**备用池**（主源挂掉时的场景）；主源健康时它会直接用主源池。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import vwap_backup as VB          # noqa: E402
from core import ext_source as ES           # noqa: E402

MARKET = 47
CAT_1MIN = 8
TIMEOUT = 8.0


def _codes(argv):
    cs = [a.upper() for a in argv if a.upper() in VB.CODES]
    return cs or list(VB.CODES)


# ---------------------------------------------------------------- 直连取数（可指定服务器组）
class _Pool:
    """绑定一组服务器、可重连的 ExHq 取数器（灾备路径专用，不经 futures_service）。"""

    def __init__(self, pairs):
        self.pairs = list(pairs)
        self.i = 0
        self.api = None
        self.last = None

    def _connect(self, ip, port):
        from pytdx.exhq import TdxExHq_API
        api = TdxExHq_API()
        try:
            if not api.connect(ip, port, time_out=TIMEOUT):
                return None
            try:
                api.client.settimeout(TIMEOUT)
            except Exception:
                pass
            return api
        except Exception:
            return None

    def get(self):
        if self.api is not None:
            return self.api
        for _ in range(len(self.pairs) or 1):
            if not self.pairs:
                return None
            ip, port = self.pairs[self.i % len(self.pairs)]
            self.i += 1
            api = self._connect(ip, port)
            if api is not None:
                self.api = api
                self.last = (ip, port)
                return api
        return None

    def fetch(self, code, start, count):
        api = self.get()
        if api is None:
            return []
        try:
            bars = api.get_instrument_bars(CAT_1MIN, MARKET, code, start, count)
        except Exception:
            try:
                api.disconnect()
            except Exception:
                pass
            self.api = None
            api = self.get()
            if api is None:
                return []
            try:
                bars = api.get_instrument_bars(CAT_1MIN, MARKET, code, start, count)
            except Exception:
                return []
        out = []
        for d in (bars or []):
            try:
                out.append({"date": str(d.get("datetime") or ""),
                            "open": float(d["open"]), "high": float(d["high"]),
                            "low": float(d["low"]), "close": float(d["close"]),
                            "vol": int(d.get("trade") or 0)})
            except Exception:
                continue
        return out

    def close(self):
        try:
            if self.api is not None:
                self.api.disconnect()
        except Exception:
            pass
        self.api = None


# ---------------------------------------------------------------- 子命令
def cmd_backup(codes, source="cli"):
    print("== backup ==", flush=True)
    out = VB.backup_all(codes, source=source)
    for c, r in out.items():
        if r.get("error"):
            print("  %-5s ERROR %s" % (c, r["error"]), flush=True)
        else:
            flag = " [源退化,保留旧最大天数 %d]" % r["max_days"] if r["kept_max"] else ""
            snap = " +snapshot" if r["snapshot"] else ""
            print("  %-5s %4d 日 (此前 %d, +%d 根)  %s..%s  异常日%d%s%s"
                  % (c, r["days"], r["days_before"], r["added_bars"],
                     r["first_day"], r["last_day"], r["short_count"], snap, flag), flush=True)
    return out


def cmd_verify(codes):
    print("== verify ==", flush=True)
    allok = True
    for c in codes:
        r = VB.verify(c, deep=True)
        allok = allok and r["ok"]
        print("  %-5s %s  备份 %d 日 / 历史 %d 日  备份最大天数记录 %s"
              % (c, "OK " if r["ok"] else "!! ", r["backup"]["days"], r["hist"]["days"],
                 (VB.manifest().get(c) or {}).get("max_days")), flush=True)
        if not r["ok"]:
            print("        %s" % r.get("advice", ""), flush=True)
            print("        备份缺口样例=%s 多余样例=%s 残缺日=%d"
                  % (r["missing_sample"], r["extra_sample"], r["backup"]["short_count"]),
                  flush=True)
    print("ALL OK" if allok else "HAS ISSUES", flush=True)
    return allok


def cmd_restore(codes, yes=False, dry_run=False):
    print("== restore ==", flush=True)
    if not yes and not dry_run:
        print("  拒绝执行：这是灾备恢复，会覆盖 data/vwap_hist。确认请加 --yes", flush=True)
        return False
    for c in codes:
        r = VB.restore(c, dry_run=dry_run)
        if r.get("ok"):
            print("  %-5s %s写入 %d 日 %d 根  %s..%s (原 %d 日, 从备份补入 %d 根, 原文件另存 %s)"
                  % (c, "would " if dry_run else "", r["written"]["days"],
                     r["written"]["n"], r["written"]["first_day"], r["written"]["last_day"],
                     r["current"]["days"], r.get("added_from_backup", 0),
                     os.path.basename(r.get("hist_bak") or "-")), flush=True)
        else:
            print("  %-5s FAILED %s" % (c, r.get("reason")), flush=True)
    return True


def cmd_heal(codes, pool="backup"):
    """灾备愈合：备份接住历史 → 指定服务器组拉增量 → 合并落盘 + 更新备份。"""
    pairs = ES.backup_pairs() if pool == "backup" else ES.primary_pairs()
    # 备用池可能很长，取延迟最优的前 8 台即可（灾备只求拿到增量）
    try:
        from core import tdx_pool_router as R
        pairs = R.select(pairs)[:8]
    except Exception:
        pairs = pairs[:8]
    print("== heal (pool=%s, %d 台) ==" % (pool, len(pairs)), flush=True)
    if not pairs:
        print("  候选为空，放弃", flush=True)
        return False
    p = _Pool(pairs)
    try:
        for c in codes:
            r = VB.heal(c, lambda s, n: p.fetch(c, s, n),
                        source="pool:%s:%s:%d" % (pool, (p.last or ("?", 0))[0],
                                                  (p.last or ("?", 0))[1]))
            print("  %-5s %d 日 → %d 日 (+%d 根, %d 轮)  末根 %s   备份 %s"
                  % (c, r["days_before"], r["days_after"], r["added_bars"], r["rounds"],
                     r["last_day_after"], r.get("backup")), flush=True)
    finally:
        p.close()
    return True


def cmd_status(codes):
    st = ES.state()
    print("== 取数源 ==", flush=True)
    print("  模式: %s%s" % (st["mode"],
                            ("  (备用态剩余 %.0fs)" % st["failover_left"])
                            if st["mode"] == "failover" else ""), flush=True)
    print("  主源锚点(历史深拉): %s %s:%d" % (st["anchor"]["name"], st["anchor"]["ip"],
                                              st["anchor"]["port"]), flush=True)
    print("  主源连续失败 %d 轮 | 累计切换 %d 次 | 主源 %d 轮 / 备用 %d 轮"
          % (st["primary_fails"], st["switch_count"], st["primary_rounds"],
             st["backup_rounds"]), flush=True)
    for n in st["primary"]:
        print("    %-15s:%d  ema=%-7s ok=%d fail=%d%s"
              % (n["ip"], n["port"], str(n["ema_ms"]), n["ok"], n["fail"],
                 " 冷却%.0fs" % n["cooldown_left"] if n["cooldown_left"] else ""), flush=True)
    print("  备用池: %d 台（主源熔断时启用）" % st["backup_count"], flush=True)
    for e in st["events"][-5:]:
        print("    事件 %s %s %s" % (time.strftime("%H:%M:%S", time.localtime(e["ts"])),
                                    e["kind"], e["detail"]), flush=True)

    print("== 备份清单 ==", flush=True)
    m = VB.manifest()
    for c in codes:
        e = m.get(c) or {}
        if not e:
            print("  %-5s 无备份" % c, flush=True)
            continue
        print("  %-5s %d 日 %d 根  最大天数记录 %d  %s..%s  ts=%s src=%s"
              % (c, e.get("days", 0), e.get("n", 0), e.get("max_days", 0),
                 e.get("first_day"), e.get("last_day"),
                 time.strftime("%m-%d %H:%M", time.localtime(e.get("ts") or 0)),
                 e.get("source") or "-"), flush=True)
    return True


def main():
    argv = sys.argv[1:]
    cmd = (argv[0] if argv and not argv[0].upper() in VB.CODES else "status").lower()
    rest = argv[1:] if cmd == argv[0].lower() else argv
    codes = _codes(rest)
    if cmd == "backup":
        cmd_backup(codes)
    elif cmd == "verify":
        cmd_verify(codes)
    elif cmd == "restore":
        cmd_restore(codes, yes=("--yes" in rest), dry_run=("--dry-run" in rest))
    elif cmd == "heal":
        pool = "primary" if "--pool=primary" in rest else "backup"
        cmd_heal(codes, pool=pool)
    else:
        cmd_status(codes)


if __name__ == "__main__":
    main()
