# -*- coding: utf-8 -*-
"""扫描「标准行情服务器池(TdxHq, 7709)」中每台主站对指定 A 股的 1 分钟历史深度，找出「最大的历史行情」。

背景
----
VWAP 深历史回补（tools/backfill_vwap_history.py）固定走国元合肥锚点单台。本项目里存在两套行情池：
  - 标准行情池 core.ak_service._TDX_HQ_SERVERS（109 台，端口 7709，A 股/指数，TdxHq_API）
  - 扩展行情池 core.tdx_ext_servers._TDX_EXT_SERVERS（32 台，端口 7721，期指/期权/港股，TdxExHq_API）

本工具默认扫**标准行情池**（A 股历史 K 线所在），对池中每一台主站独立探测其 1 分钟历史的最早
日期与跨度，排序后即可知道「哪台服务器保留了最大的历史行情」。

PyTDX 取数语义（实测确认，两种后端一致）
----------------------------------------
  get_security_bars(cat, market, code, start, count)  /  get_instrument_bars(...)
  - start=0 取的是**最新**一页（距今天最近的那 count 根）。
  - start 增大 = 向历史回退；start 越过总量即返回空。
  - 单次最多返回约 700 根；不足 700 即「半页」，是到达历史最前端的可靠信号。

方法（对齐 VWAP 的「服务器池 + 分页拉取」思路，但只测深度、绝不混用数据）
------------------------------------------------------------------
  - 对每台主站先做 TCP 可达性预检（短超时），不可达直接标记 UNREACHABLE，不做 25 次徒劳探测。
  - 单连接复用探测：
      1) p0 = q(0)            -> 最新一页，latest = p0[-1]
      2) 若 len(p0) < 700     -> 全量不足一页，直接 earliest/latest/total 全得
      3) 倍增找上界 hi，使 q(hi) 空/半页
      4) 二分找「最大的整页 start」S（len==700 即为整页，单调）
      5) 末端半页 q(S+700)    -> 最旧一页，earliest = 末页[-1]，total = S+700+len(末页)
    全程约 25 请求/台；每次取数带重试（瞬断不误判成历史末端）。
  - 并发探测 + 单台超时，避免把券商主站打爆；失败/超时标记，不影响其余。

⚠️ 为何只测不取：不同站同 bar 的高/低/量存在微差，历史页跨站混用会让 ATR→dev_z 漂移、信号翻转
（2026-09-21 事故）。本工具仅做只读深度探测，不落盘任何历史数据，故无污染风险。

输出
----
  - 终端按「历史根数(total_bars)」降序打表（最深在前）；「最大的历史行情」= 表首那台。
  - 落盘 JSON 报告（默认 data/pool_scan_<backend>_<code>.json）。

用法
----
  python tools/scan_pool_history.py                              # 默认：标准行情池 + 600000.SH
  python tools/scan_pool_history.py --code 000001.SZ             # 指定股票
  python tools/scan_pool_history.py --backend ext --code IFL9    # 扫扩展行情池(期指)
  python tools/scan_pool_history.py --limit 10                   # 只扫前 10 台（快速验证）
  python tools/scan_pool_history.py --top 10                     # 只看前 10 深
  python tools/scan_pool_history.py --selftest                  # 不联网，用合成数据验证二分算法
  python tools/scan_pool_history.py --cat 0                     # 扫 5 分钟历史深度（cat=0）
  python tools/scan_pool_history.py --cat 1 --code 600000.SH    # 扫 15 分钟（cat=1）
  python tools/scan_pool_history.py --cat 2                     # 扫 30 分钟（cat=2）

PyTDX TdxHq category 速查（与 core/ak_service._PERIOD_CAT 一致）
-------------------------------------------------------------
  0 = 5 分钟  1 = 15 分钟  2 = 30 分钟  3 = 1 小时
  4 = 日线    5 = 周线     6 = 月线     7 = 1 分钟  9 = 日线(新)
"""
import os
import sys
import time
import json
import socket
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 单次 get_*_bars 取数根数（服务端分页上限 ~700；不足即半页=到头信号）
_PAGE = 700
# 二分查找历史上界初值（期指最长 ~37 万根，A 股 1min ~5 万根，均 < 50 万）
_GUESS_HI = 500_000
# TCP 可达性预检超时（秒）
_REACH_TO = 2.0
# 单台连接/取数超时（秒）
_TIMEOUT = 5.0
# 取数重试次数（瞬断容错）
_RETRIES = 2
# 并发探测台数（避免把券商主站打爆；也别太低否则扫描太久）
_WORKERS = 12


def _span_days(earliest, latest):
    """由最早/最晚日期字符串("YYYY-MM-DD ...")计算日历跨度(天, 含首尾)。"""
    from datetime import datetime as _dt
    try:
        d0 = _dt.strptime(earliest[:10], "%Y-%m-%d").date()
        d1 = _dt.strptime(latest[:10], "%Y-%m-%d").date()
        return max(1, (d1 - d0).days + 1)
    except Exception:
        return 0


# =========================================================
# 取数函数构造（含重试：瞬断不误判成历史末端）
# =========================================================
def _q_with_retry(api_factory, call_fn, ip, port, timeout):
    """带重试执行一次取数；返回 list[datetime_str]，任何失败返回 []。"""
    last = []
    for _ in range(_RETRIES + 1):
        api = None
        try:
            api = api_factory()
            if not api.connect(ip, port, time_out=timeout):
                continue
            try:
                api.client.settimeout(timeout)
            except Exception:
                pass
            bars = call_fn(api)
            last = [str((b.get("datetime") or b.get("date") or "")) for b in (bars or [])]
            return last
        except Exception:
            last = []
        finally:
            try:
                if api is not None:
                    api.disconnect()
            except Exception:
                pass
    return last


def _make_ext_query(ip, port, code, timeout, cat=8):
    """扩展行情(期指)单台探测：q(start, count) -> [datetime_str, ...]。cat 默认 8=1分钟。"""
    from pytdx.exhq import TdxExHq_API
    MARKET = 47
    mkt_code = code.upper()

    def q(start, count):
        def call(api):
            return api.get_instrument_bars(cat, MARKET, mkt_code, start, count)
        return _q_with_retry(TdxExHq_API, call, ip, port, timeout)

    return q


def _market_of(code):
    """A 股代码 -> TdxHq 市场代码：沪市=1，深市=0。"""
    c = code.upper().split(".")[0]
    if c.startswith("6"):
        return 1
    if c.startswith(("0", "3")):
        return 0
    return 1  # 默认沪市


def _make_stock_query(ip, port, code, timeout, cat=7):
    """A 股 TdxHq 单台探测：q(start, count) -> [datetime_str, ...]。

    cat: PyTDX category（默认 7=1分钟；0=5分钟,1=15分钟,2=30分钟,3=1小时,9=日线）。
    算法只依赖 start 语义（start=0=最新端），与周期无关，故同一套可扫任意分钟线。
    """
    from pytdx.hq import TdxHq_API
    mkt = _market_of(code)
    c6 = code.split(".")[0].zfill(6)

    def q(start, count):
        def call(api):
            return api.get_security_bars(cat, mkt, c6, start, count)
        return _q_with_retry(TdxHq_API, call, ip, port, timeout)

    return q


# =========================================================
# 二分查找历史深度（start=0=最新端；找最旧端）
# =========================================================
def measure_depth(q, page=_PAGE, guess_hi=_GUESS_HI):
    """对单台主站探测 1 分钟历史深度。

    返回 dict: {ok, earliest, latest, total_bars, span_days}；不可达/无数据返回 ok=False。

    算法（实测语义：start=0 最新端，start 增大向历史回退，越过末端即空；
          且每页**时间升序**——页内首根最旧、末根最新）：
      1) p0 = q(0, page)              —— 最新一页；空则本台无该代码数据。
      2) latest = p0[-1]              —— 最新日期（页末位=最新）。
      3) 若 len(p0) < page            —— 全量不足一页：earliest=p0[0], total=len(p0)。
      4) 倍增找上界 hi：q(hi) 非空则翻倍，空/半页即停。
      5) 二分找「最大的非空 start」L（单调：start∈[0,T) 非空，≥T 空）。
         pL = q(L, page) 即最旧一页：首根 pL[0] = 绝对最旧 bar。
         total = L + len(pL)；earliest = pL[0]。
      6) 验证：q(L) 非空且 q(L+1) 空；若 q(L+1) 也非空（搜索期瞬断致欠估），
         把 L 上移重搜，直至边界稳定。
    """
    p0 = q(0, page)
    if not p0:
        return {"ok": False, "reason": "no-data@start0"}
    latest = p0[-1]
    if len(p0) < page:
        # 全量不足一页：p0 本身即 [最旧..最新]
        return {
            "ok": True, "earliest": p0[0], "latest": latest,
            "total_bars": len(p0),
            "span_days": _span_days(p0[0], latest),
            "boundary_start": 0,
        }

    # 倍增找上界：只要 q(hi) 非空（整页/半页都算）就继续翻倍，直到 q(hi) 空=越过末端。
    # 注意：半页只代表已逼近末端、并未越过，绝不能在此停下，否则 hi 会小于真实总量。
    hi = page
    while True:
        ph = q(hi, page)
        if not ph:
            break  # 越过历史末端
        hi *= 2
        if hi > guess_hi * 4:
            break

    # 二分：最大的非空 start ∈ [0, hi]
    L = -1
    a, b = 0, hi
    while a <= b:
        mid = (a + b) // 2
        pm = q(mid, page)
        if pm:
            L = mid
            a = mid + 1
        else:
            b = mid - 1

    # 边界校正（排除搜索期瞬断导致的非单调）：最多 3 轮，安全重搜而非盲目跳转。
    for _ in range(3):
        if L < 0:
            break
        pL = q(L, page)
        nxt = q(L + 1, page)
        if not pL and nxt:
            # L 越界（q(L) 竟空但 q(L+1) 非空）→ 回退到 [0, L-1] 重搜
            a, b, L = 0, L - 1, -1
            while a <= b:
                mid = (a + b) // 2
                if q(mid, page):
                    L = mid
                    a = mid + 1
                else:
                    b = mid - 1
        elif pL and nxt:
            # L 非最大（q(L+1) 仍非空）→ 上移到 [L+1, hi] 重搜
            a, b, L = L + 1, hi, L
            while a <= b:
                mid = (a + b) // 2
                if q(mid, page):
                    L = mid
                    a = mid + 1
                else:
                    b = mid - 1
        else:
            break  # q(L) 非空且 q(L+1) 空 -> 稳定边界

    if L < 0:
        return {"ok": False, "reason": "no-boundary"}
    pL = q(L, page)
    if not pL:
        return {"ok": False, "reason": "boundary-empty"}
    earliest = pL[0]            # 最旧页首根 = 绝对最旧
    total = L + len(pL)

    days = _span_days(earliest, latest)
    return {
        "ok": True,
        "earliest": earliest,
        "latest": latest,
        "total_bars": total,
        "span_days": days,
        "boundary_start": L,
    }


# =========================================================
# 单台探测 worker（TCP 预检 + 深度测量 + 计时）
# =========================================================
def _reachable(ip, port, timeout=_REACH_TO):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _probe_one(name, ip, port, code, backend, timeout, cat):
    make = _make_ext_query if backend == "ext" else _make_stock_query
    q = make(ip, port, code, timeout, cat)
    t0 = time.time()
    if not _reachable(ip, port):
        return {"name": name, "ip": ip, "port": port, "ok": False,
                "reason": "TCP_UNREACHABLE", "elapsed_ms": int((time.time() - t0) * 1000)}
    try:
        res = measure_depth(q, page=_PAGE, guess_hi=_GUESS_HI)
    except Exception as e:
        return {"name": name, "ip": ip, "port": port, "ok": False,
                "reason": "exception:%s" % e, "elapsed_ms": int((time.time() - t0) * 1000)}
    res.update({"name": name, "ip": ip, "port": port,
                "elapsed_ms": int((time.time() - t0) * 1000)})
    return res


# =========================================================
# 服务器清单解析
# =========================================================
def _load_servers(backend, pool_file, limit):
    if pool_file:
        servers = []
        with open(pool_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    servers.append((parts[0], parts[1], int(parts[2])))
        return servers[:limit] if limit else servers

    if backend == "ext":
        try:
            from core.tdx_ext_servers import _TDX_EXT_SERVERS
        except Exception:
            try:
                from tdx_ext_servers import _TDX_EXT_SERVERS
            except Exception as e:
                raise RuntimeError("无法加载扩展行情池 core/tdx_ext_servers：%r" % e)
        servers = list(_TDX_EXT_SERVERS)
    else:  # stock
        try:
            from core.ak_service import _TDX_HQ_SERVERS
        except Exception as e:
            raise RuntimeError(
                "无法加载 A 股行情池 core/ak_service._TDX_HQ_SERVERS（可能 akshare 未装）：%r\n"
                "可在完整环境运行，或用 --pool-file 传入 name,ip,port 清单。" % e)
        servers = list(_TDX_HQ_SERVERS)
    return servers[:limit] if limit else servers


# =========================================================
# 报告输出
# =========================================================
def _print_table(rows, top=None):
    ok_rows = [r for r in rows if r.get("ok")]
    bad_rows = [r for r in rows if not r.get("ok")]
    # 最深 = 历史根数最多（earliest 最早并列时以 earliest 日期更早优先）
    ok_rows.sort(key=lambda r: (r.get("total_bars") or 0,
                                r.get("earliest") or "9999"), reverse=True)

    show = ok_rows if top is None else ok_rows[:top]
    print("\n================ 历史深度排名（根数最多在前 = 最大的历史行情）================")
    print("%-22s %-18s %10s %12s %12s %8s %8s" %
          ("server", "ip:port", "根数", "最早", "最晚", "天数", "ms"))
    for r in show:
        print("%-22s %-18s %10d %12s %12s %8s %8d" % (
            r["name"][:22], "%s:%d" % (r["ip"], r["port"]),
            r["total_bars"], r["earliest"][:10], r["latest"][:10],
            r["span_days"], r["elapsed_ms"]))
    if not show:
        print("  （无可达主站返回数据）")
    print("----------------------------------------------------------------------")
    print("可达且有数据: %d / %d 台" % (len(ok_rows), len(rows)))
    if ok_rows:
        top1 = ok_rows[0]
        print("★ 最大的历史行情: %s (%s:%d) — %d 根, 最早 %s" % (
            top1["name"], top1["ip"], top1["port"],
            top1["total_bars"], top1["earliest"][:10]))
    if bad_rows:
        unreach = sum(1 for r in bad_rows if r.get("reason") == "TCP_UNREACHABLE")
        print("\n---- 不可达 / 无数据（共 %d 台，其中 TCP 不可达 %d，前 15）----" %
              (len(bad_rows), unreach))
        for r in bad_rows[:15]:
            print("  %-22s %-18s %s" % (r["name"][:22], "%s:%d" % (r["ip"], r["port"]),
                                        r.get("reason", "unreachable")))
        if len(bad_rows) > 15:
            print("  ... 其余 %d 台省略" % (len(bad_rows) - 15))


def _selftest():
    """用合成历史验证二分查找算法（不联网）。

    合成语义对齐真实：index 0 = 最新，index i = 最新 - i 分钟（i 越大越旧）。
    q(start) 返回 [start, start+len) 对应的 datetime 字符串。
    """
    from datetime import datetime, timedelta
    N = 368_480  # 模拟 IFL9 全量根数
    newest = datetime(2026, 9, 24, 15, 0)

    def dt_at(i):
        return newest - timedelta(minutes=i)

    def _as_str(i):
        return dt_at(i).strftime("%Y-%m-%d %H:%M")

    def _page(start, count, nmax):
        """对齐真实 API：返回页为时间升序；页内首根=最旧(最大 offset)，末根=最新(最小 offset)。"""
        if start < 0 or start >= nmax:
            return []
        end = min(start + count, nmax)
        # offsets 从大到小排列 => 时间升序；list[0]=dt_at(end-1)(最旧), list[-1]=dt_at(start)(最新)
        return [_as_str(i) for i in range(end - 1, start - 1, -1)]

    def q(start, count):
        return _page(start, count, N)

    res = measure_depth(q, page=_PAGE, guess_hi=_GUESS_HI)
    assert res["ok"], "selftest: 期望 ok"
    assert res["total_bars"] == N, "selftest: total_bars 错误 = %s" % res.get("total_bars")
    assert res["latest"] == _as_str(0), "selftest: latest 错误 = %s" % res.get("latest")
    assert res["earliest"] == _as_str(N - 1), "selftest: earliest 错误 = %s" % res.get("earliest")
    print("[selftest] OK: total=%d earliest=%s latest=%s" % (
        res["total_bars"], res["earliest"], res["latest"]))

    # 边界：极小历史（仅 50 根，不足一页）
    M = 50

    def q_small(start, count):
        return _page(start, count, M)

    res2 = measure_depth(q_small, page=_PAGE, guess_hi=_GUESS_HI)
    assert res2["ok"] and res2["total_bars"] == M, "selftest: 小历史失败 %s" % res2
    assert res2["earliest"] == _as_str(M - 1) and res2["latest"] == _as_str(0), \
        "selftest: 小历史端点错 %s..%s" % (res2["earliest"], res2["latest"])
    print("[selftest] OK(small): total=%d" % res2["total_bars"])

    # 边界：恰为 page 整数倍（total = 2*page）
    K = 2 * _PAGE

    def q_k(start, count):
        return _page(start, count, K)

    res3 = measure_depth(q_k, page=_PAGE, guess_hi=_GUESS_HI)
    assert res3["ok"] and res3["total_bars"] == K, "selftest: 整数倍失败 %s" % res3
    assert res3["earliest"] == _as_str(K - 1), "selftest: 整数倍 earliest 错 %s" % res3.get("earliest")
    print("[selftest] OK(exact-multiple): total=%d" % res3["total_bars"])
    return True


def main():
    ap = argparse.ArgumentParser(
        description="扫描标准行情服务器池，找出保留最大历史行情的主站（默认 A 股池 / 1 分钟）")
    ap.add_argument("--backend", choices=["ext", "stock"], default="stock",
                    help="stock=标准行情池(A股/指数, 默认) ；ext=扩展行情池(期指/期权)")
    ap.add_argument("--cat", type=int, default=7,
                    help="PyTDX category：7=1分钟(默认) 0=5分钟 1=15分钟 2=30分钟 3=1小时 9=日线")
    ap.add_argument("--code", default="600000.SH",
                    help="股票代码：默认 600000.SH；ext 用 IFL9 等合约")
    ap.add_argument("--limit", type=int, default=None,
                    help="只扫前 N 台（快速验证用，不传则扫全池）")
    ap.add_argument("--top", type=int, default=None, help="只显示排名前 N 深")
    ap.add_argument("--timeout", type=float, default=_TIMEOUT, help="单台连接/取数超时(秒)")
    ap.add_argument("--workers", type=int, default=_WORKERS, help="并发探测台数")
    ap.add_argument("--out", default=None, help="JSON 报告输出路径")
    ap.add_argument("--pool-file", default=None,
                    help="自定义服务器清单文件：每行 name,ip,port（覆盖内置池）")
    ap.add_argument("--selftest", action="store_true", help="仅运行二分算法自测（不联网）")
    args = ap.parse_args()

    if args.selftest:
        _selftest()
        return

    try:
        servers = _load_servers(args.backend, args.pool_file, args.limit)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)

    print("后端=%s 代码=%s cat=%d 待探测主站=%d 台  并发=%d  超时=%.1fs" % (
        args.backend, args.code, args.cat, len(servers), args.workers, args.timeout))

    results = []
    with ThreadPoolExecutor(max_workers=min(args.workers, len(servers) or 1)) as ex:
        futs = [ex.submit(_probe_one, n, ip, port, args.code, args.backend, args.timeout, args.cat)
                for (n, ip, port) in servers]
        for f in as_completed(futs):
            try:
                r = f.result()
            except Exception as e:
                r = {"ok": False, "reason": "future-exc:%s" % e}
            results.append(r)
            if r.get("ok"):
                print("  ✓ %-22s %-18s %d 根  最早 %s  最晚 %s" % (
                    r["name"][:22], "%s:%d" % (r["ip"], r["port"]),
                    r["total_bars"], r["earliest"][:10], r["latest"][:10]))
            else:
                print("  ✗ %-22s %-18s %s" % (
                    r.get("name", "?")[:22],
                    "%s:%s" % (r.get("ip", "?"), r.get("port", "?")),
                    r.get("reason", "unreachable")))

    _print_table(results, top=args.top)

    out = args.out or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "pool_scan_%s_cat%d_%s.json" % (args.backend, args.cat, args.code.replace(".", "")))
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump({
                "backend": args.backend, "code": args.code, "cat": args.cat,
                "scanned": len(results),
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "results": results,
            }, f, ensure_ascii=False, indent=2)
        print("\nJSON 报告已写：%s" % out)
    except Exception as e:
        print("JSON 写盘失败：%r" % e)


if __name__ == "__main__":
    main()
