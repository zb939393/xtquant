# -*- coding: utf-8 -*-
"""回补 VWAP 看板所需的「最大量」1 分钟历史到 data/vwap_hist/<code>_1min.pkl。

背景（2026-09-22 实测）：
  - 扩展行情池 28/32 台可连；除银河 7730（仅 712d）外，均在 1,495~1,538 交易日。
  - 最深 = 国元证券合肥站 5 台，IFL9 可回溯至 2020-05-28（368,480 根 / 1,536 日）。
  - 各品种深度受「合约上市日」限制：IFL9 1,536d / IHL9 1,505d / ICL9 1,504d /
    IML9 1,012d（中证1000 期指 2022-07 才上市）。

取数源（2026-09-22 收敛）：
  **只走国元合肥主源锚点单台**（core/ext_source.ANCHOR = 220.248.233.5:7721）。
  5 台之间的深历史与同 bar 数值有微差，历史页跨站混用会让 ATR→dev_z 漂移、信号翻转，
  故这里绝不轮转、也不自动降级到其他站；锚点连不上就停，改走灾备流程：
      python tools/vwap_backup.py restore --yes   # 备份接住历史
      python tools/vwap_backup.py heal            # 备用池补增量

回补完成后自动刷新备份（core/vwap_backup，并集合并、天数只增不减）。

用法：
  python tools/backfill_vwap_history.py            # 四品种，断点续传
  python tools/backfill_vwap_history.py IFL9       # 单品种
"""
import os
import sys
import time
import threading
import pickle

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pytdx.exhq import TdxExHq_API  # noqa: E402

MARKET = 47
CAT_1MIN = 8
COUNT = 700
TIMEOUT = 6.0
MAX_PAGES = 1200                      # 上限保护（1200*700=84万根，远超实际）

_OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "vwap_hist")

# ⚠️ 历史深拉**只认国元合肥主源锚点单台**，绝不在 5 台之间轮转：
#    5 台虽同券商同机房，但深历史略有差异（2026-09-22 实测：联通2/移动2 至 2020-07-17，
#    其余 3 台至 2020-07-20），且同一根已收盘 bar 的高/低/量存在微差。历史页跨站混用
#    → ATR 递归链 → dev_atr → dev_z 漂移 → 已发生的信号翻转（2026-09-21 实测事故）。
#    锚点定义与取数源策略见 core/ext_source.py（ANCHOR）。
try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.ext_source import ANCHOR as _ANCHOR, anchor_name as _anchor_name
except Exception:
    _ANCHOR, _anchor_name = ("220.248.233.5", 7721), lambda: "国元合肥联通1"

_SERVERS = [(_anchor_name(), _ANCHOR[0], _ANCHOR[1])]
_SERVER_RETRY = 4        # 锚点连接重试次数（单台抖动时原地重试，不换站）


class Conn:
    """一个可自动重连的 ExHq 连接。"""

    def __init__(self, servers):
        self.servers = list(servers)
        self.i = 0
        self.api = None

    def _connect(self, name, ip, port):
        api = TdxExHq_API()
        if not api.connect(ip, port, time_out=TIMEOUT):
            return None
        try:
            api.client.settimeout(TIMEOUT)
        except Exception:
            pass
        return api

    def get(self):
        if self.api is not None:
            return self.api
        for _ in range(len(self.servers)):
            name, ip, port = self.servers[self.i % len(self.servers)]
            self.i += 1
            api = self._connect(name, ip, port)
            if api is not None:
                self.api = api
                return api
        return None

    def drop(self):
        try:
            if self.api is not None:
                self.api.disconnect()
        except Exception:
            pass
        self.api = None


def _out_path(code):
    return os.path.join(_OUT_DIR, "%s_1min.pkl" % code)


def _load(code):
    try:
        with open(_out_path(code), "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _save(code, bars, source):
    os.makedirs(_OUT_DIR, exist_ok=True)
    tmp = _out_path(code) + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"bars": bars, "ts": time.time(), "source": source, "code": code}, f)
    os.replace(tmp, _out_path(code))


# ---------------------------------------------------------------- 增量落盘（delta）
# 回补过程不再每 20 页全量 pickle 整个 36 万根（约 18s/品种），改为把每页**新增** bar 追加
# 写到一个 .delta 文件（小批 pickle，代价可忽略）。仅正常结束 / 崩溃中断时才碰主 pkl：
#   - 正常结束：合并 主pkl ∪ delta → 原子写主 pkl 一次 → 删 delta；
#   - 崩溃中断：delta 保留，下次 resume 读入继续（与已落盘主 pkl 按 date 去重合并，旧值优先）。
def _delta_path(code):
    return _out_path(code) + ".delta"


def _read_delta(code):
    """读回补中断遗留的增量批次，合并成 bars 列表（损坏则忽略，绝不抛异常）。"""
    p = _delta_path(code)
    bars = []
    try:
        with open(p, "rb") as f:
            while True:
                try:
                    batch = pickle.load(f)
                    if isinstance(batch, list):
                        bars.extend(batch)
                except EOFError:
                    break
                except Exception:
                    break
    except FileNotFoundError:
        return []
    except Exception:
        return bars
    return bars


def _append_delta(fh, batch):
    if not batch:
        return
    pickle.dump(batch, fh)
    fh.flush()


def _clear_delta(code):
    try:
        os.remove(_delta_path(code))
    except OSError:
        pass


def backfill(code):
    servers = list(_SERVERS)          # 只认锚点单台（见文件头 ⚠️ 说明），不跨站混用
    conn = Conn(servers)

    # ---- resume：主 pkl ∪ 中断遗留 delta，按 date 去重（旧值优先）----
    obj = _load(code)
    idx = {}
    if obj and obj.get("bars"):
        for b in obj["bars"]:
            idx[b["date"]] = b
    for b in _read_delta(code):
        idx.setdefault(b["date"], b)
    have = len(idx)

    dfh = open(_delta_path(code), "ab")   # 续写 / 新建增量文件
    print("[%s] start (resume from %d bars, anchor=%s)"
          % (code, have, servers[0][0]), flush=True)

    start = have                          # 续传点：已落盘 bar 数（去重保证安全，不会重复拉）
    pages = 0
    new = 0
    while start < MAX_PAGES * COUNT:
        api = conn.get()
        if api is None:
            print("[%s] ANCHOR_DOWN, stop at start=%d —— 锚点 %s 连不上；"
                  "不要换其他站续拉（会污染历史），请改用灾备流程：\n"
                  "    python tools/vwap_backup.py restore --yes   # 先用备份接住历史\n"
                  "    python tools/vwap_backup.py heal            # 再用备用池补增量"
                  % (code, start, servers[0][0]), flush=True)
            break
        bars = None
        for attempt in range(_SERVER_RETRY):
            try:
                bars = api.get_instrument_bars(CAT_1MIN, MARKET, code, start, COUNT)
                break
            except Exception:
                conn.drop()
                api = conn.get()
                if api is None:
                    break
        if bars is None:
            print("[%s] fetch err, stop at start=%d" % (code, start), flush=True)
            break
        if not bars:
            print("[%s] reached end at start=%d (total %d bars)" % (code, start, len(idx)),
                  flush=True)
            break
        batch = []
        for d in bars:
            dt = str(d.get("datetime") or "")
            if not dt:
                continue
            try:
                rec = {"date": dt, "open": float(d["open"]), "high": float(d["high"]),
                       "low": float(d["low"]), "close": float(d["close"]),
                       "vol": int(d.get("trade") or 0)}
            except Exception:
                continue
            if dt not in idx:
                idx[dt] = rec
                new += 1
                batch.append(rec)
        _append_delta(dfh, batch)          # 仅增量落盘，代价可忽略（不再每 20 页全量 pickle）
        pages += 1
        start += len(bars)
        if pages % 20 == 0:
            print("[%s] page %d start=%d total=%d (+%d)"
                  % (code, pages, start, len(idx), new), flush=True)
        if len(bars) < COUNT:
            print("[%s] last partial page (%d) at start=%d" % (code, len(bars), start), flush=True)
            break

    dfh.close()

    # ---- 结束：合并写主 pkl 一次 + 清 delta ----
    bars_sorted = sorted(idx.values(), key=lambda x: x["date"])
    _save(code, bars_sorted, "%s:%d" % (servers[0][1], servers[0][2]))
    _clear_delta(code)
    days = len(set(b["date"][:10] for b in bars_sorted))
    rng = (bars_sorted[0]["date"], bars_sorted[-1]["date"]) if bars_sorted else ("-", "-")
    print("[%s] DONE bars=%d days=%d range=%s..%s" % (code, len(bars_sorted), days, rng[0], rng[1]),
          flush=True)
    conn.drop()

    # 回补完成即刷新备份（并集合并，天数只增不减），保证「最大数据天数」随时可恢复
    try:
        from core import vwap_backup as _vb
        r = _vb.backup_code(code, source="backfill:%s" % servers[0][0])
        print("[%s] backup: %d 日 (最大天数记录 %d, +%d 根)"
              % (code, r["days"], r["max_days"], r["added_bars"]), flush=True)
    except Exception as e:
        print("[%s] backup skipped: %r" % (code, e), flush=True)


def main():
    codes = sys.argv[1:] or ["IFL9", "IHL9", "ICL9", "IML9"]
    ths = [threading.Thread(target=backfill, args=(c,)) for c in codes]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
