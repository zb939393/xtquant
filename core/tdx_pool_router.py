# -*- coding: utf-8 -*-
"""扩展行情主站池的智能选路：延迟排序优先 + 故障节点临时降权。

背景：池已扩到 32 台，纯 random.shuffle 选路有两个问题：
  1) 慢节点（数百 ms）与快节点（20ms）被等概率选中，P50/P95 延迟被慢节点拖尾；
  2) 故障节点不会被记住，每次请求都可能再踩一遍，失败重试进一步放大延迟。

本模块维护每台节点的运行期统计（EMA 延迟 / 连续失败 / 冷却到期 / 成功失败计数），
对外提供：
  - select(pairs)                 按健康度排序的候选列表（Top-K 内随机，避免全部流量打爆最快那台）
  - report_success(ip, port, ms)  更新 EMA 延迟、清零连续失败、解除冷却
  - report_failure(ip, port)      连续失败达阈值 → 冷却指数退避（30s→60s→…→600s 封顶）
  - maybe_warmup(pairs, probe)    冷启动 / 间隔到期时后台异步预热探测，先测延迟再排序
  - snapshot()                    运维视图：每台的 ema / fails / cooldown / ok / fail

设计取舍：
  - **Top-K 内随机**而非「永远选最快」：纯最优会让 32 台退化成 1 台，反而把最快节点打爆；
    取前 K 台再 shuffle，兼顾「优先快的」与「负载分散」。
  - **冷却用指数退避 + 上限**：故障节点不是永久拉黑（券商主站多为临时抖动/维护），
    冷却到期后自动回到候选池重新参与排序。
  - **全冷却兜底**：万一所有节点同时被降权（如本机断网），放开全部候选再试一遍，
    绝不因为统计状态而返回空列表。

线程安全（全局 RLock）；刻意不 import 任何重依赖（pytdx / akshare），
以便 futures_service / option_exquote_service 共用且不触发全量导入。
"""
import random
import threading
import time

# ---------------------------------------------------------------- 可调参数
_EMA_ALPHA = 0.3           # EMA 平滑系数：新样本权重（越大越快反映当前延迟）
_DEFAULT_MS = 150.0        # 未测过节点的初始估值（中性，不抢占也不垫底）
_FAIL_THRESHOLD = 2        # 连续失败达此次数进入冷却
_COOLDOWN_BASE = 30.0      # 首次冷却时长（秒）
_COOLDOWN_MAX = 600.0      # 冷却上限（秒）
_FAIL_PENALTY_MS = 20.0    # 每次连续失败叠加的排序惩罚（未达冷却阈值时也略微靠后）
_TOPK = 5                  # 从前 K 台里随机选，避免单点过热
_WARMUP_INTERVAL = 1800.0  # 两次预热探测的最小间隔（秒）

_LOCK = threading.RLock()
_STATS = {}                # (ip, port) -> dict
_WARMUP = {"ts": 0.0, "running": False}
_WARMUP_LOCK = threading.Lock()


def _key(ip, port):
    return (str(ip), int(port))


def _rec(ip, port):
    k = _key(ip, port)
    st = _STATS.get(k)
    if st is None:
        st = {
            "ema": None,        # EMA 延迟（毫秒），None = 从未测过
            "fails": 0,         # 连续失败次数（成功即清零）
            "ok": 0,            # 累计成功
            "fail": 0,          # 累计失败
            "cooldown_until": 0.0,
            "last_ok": 0.0,
            "last_probe": 0.0,
        }
        _STATS[k] = st
    return st


# ---------------------------------------------------------------- 上报
def report_success(ip, port, elapsed_ms=None):
    """取数/连接成功：更新延迟 EMA、清零连续失败并解除冷却。"""
    now = time.time()
    try:
        ms = float(elapsed_ms)
    except Exception:
        ms = None
    with _LOCK:
        st = _rec(ip, port)
        st["fails"] = 0
        st["cooldown_until"] = 0.0
        st["ok"] += 1
        st["last_ok"] = now
        if ms is not None and ms >= 0:
            if st["ema"] is None:
                st["ema"] = ms
            else:
                st["ema"] = (1.0 - _EMA_ALPHA) * st["ema"] + _EMA_ALPHA * ms


def report_failure(ip, port):
    """连接/取数失败：连续失败达阈值即冷却（指数退避，上限 _COOLDOWN_MAX）。"""
    now = time.time()
    with _LOCK:
        st = _rec(ip, port)
        st["fails"] += 1
        st["fail"] += 1
        if st["fails"] >= _FAIL_THRESHOLD:
            n = st["fails"] - _FAIL_THRESHOLD
            cd = min(_COOLDOWN_BASE * (2 ** n), _COOLDOWN_MAX)
            st["cooldown_until"] = now + cd
    return st["fails"]


def is_cooled(ip, port):
    with _LOCK:
        st = _STATS.get(_key(ip, port))
        return bool(st and st["cooldown_until"] > time.time())


# ---------------------------------------------------------------- 选路
def _score(st, now):
    """越小越优先。冷却中的节点返回 >= _COOLED_BASE，视为不可用。"""
    if st is None:
        return _DEFAULT_MS
    ema = st["ema"] if st["ema"] is not None else _DEFAULT_MS
    base = ema + min(st["fails"], 10) * _FAIL_PENALTY_MS
    if st["cooldown_until"] > now:
        base += 1e9
    return base


_COOLED_BASE = 1e9


def select(pairs, topk=_TOPK, include_cooled=False):
    """按健康度排序返回候选 (ip, port) 列表；Top-K 内随机以分散负载。

    pairs: [(ip, port), ...]
    include_cooled: True 时不做冷却过滤（一般不要开）。
    兜底：若全部节点都处于冷却（如本机断网），放开全部候选，绝不返回空列表。
    """
    now = time.time()
    with _LOCK:
        scored = []
        for pair in pairs:
            try:
                ip, port = pair[0], pair[1]
            except Exception:
                continue
            k = _key(ip, port)
            scored.append((_score(_STATS.get(k), now), k))
    # 同分时用随机打破平局，避免冷启动时每台顺序恒定
    scored.sort(key=lambda x: (x[0], random.random()))

    keys = [k for (_s, k) in scored]
    if not include_cooled:
        alive = [k for (s, k) in scored if s < _COOLED_BASE]
        if alive:
            keys = alive
        # 全部冷却 → 保持 keys（放开全部，保底重试）

    k = max(1, min(int(topk), len(keys)))
    head = keys[:k]
    random.shuffle(head)
    return head + keys[k:]


# ---------------------------------------------------------------- 预热探测
def _warmup_worker(pairs, probe, interval):
    try:
        from concurrent.futures import ThreadPoolExecutor
    except Exception:
        ThreadPoolExecutor = None

    def _one(pair):
        ip, port = pair[0], pair[1]
        # 冷却期内的节点不探测：否则每次预热都会给它续期，冷却永远解不开。
        if is_cooled(ip, port):
            return (ip, port, None)
        try:
            ms = probe(ip, port)
        except Exception:
            ms = None
        # 由 worker 统一上报（probe 只负责测量并返回毫秒 / None），避免双重计数
        if ms is None:
            report_failure(ip, port)
        else:
            report_success(ip, port, ms)
        with _LOCK:
            st = _rec(ip, port)
            st["last_probe"] = time.time()
        return (ip, port, ms)

    try:
        if ThreadPoolExecutor is not None:
            with ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(_one, pairs))
        else:
            for p in pairs:
                _one(p)
    except Exception:
        pass
    finally:
        with _WARMUP_LOCK:
            _WARMUP["running"] = False
            _WARMUP["ts"] = time.time()


def maybe_warmup(pairs, probe, interval=_WARMUP_INTERVAL, force=False):
    """冷启动或距上次预热超过 interval 时，后台异步探测一次各节点延迟。

    probe(ip, port) -> 成功返回耗时毫秒，失败返回 None（内部自行 report_failure）。
    非阻塞：仅在条件满足时起一个 daemon 线程，不影响当前请求。
    """
    if not pairs or probe is None:
        return False
    now = time.time()
    with _WARMUP_LOCK:
        if _WARMUP["running"]:
            return False
        if not force and (now - _WARMUP["ts"]) < interval:
            return False
        _WARMUP["running"] = True
    try:
        t = threading.Thread(
            target=_warmup_worker,
            args=(list(pairs), probe, interval),
            daemon=True,
            name="tdx-pool-warmup",
        )
        t.start()
        return True
    except Exception:
        with _WARMUP_LOCK:
            _WARMUP["running"] = False
        return False


# ---------------------------------------------------------------- 运维视图
def snapshot():
    """返回每台节点的统计快照（按优先级排序），供日志 / 运维接口使用。"""
    now = time.time()
    with _LOCK:
        items = []
        for (ip, port), st in _STATS.items():
            items.append({
                "ip": ip,
                "port": port,
                "ema_ms": None if st["ema"] is None else round(st["ema"], 1),
                "fails": st["fails"],
                "ok": st["ok"],
                "fail": st["fail"],
                "cooldown_left": max(0.0, round(st["cooldown_until"] - now, 1)),
                "last_ok": st["last_ok"],
            })
    items.sort(key=lambda d: (
        d["cooldown_left"] > 0,
        d["ema_ms"] if d["ema_ms"] is not None else _DEFAULT_MS,
    ))
    return items


def reset():
    """清空统计（仅供测试）。"""
    with _LOCK:
        _STATS.clear()
    with _WARMUP_LOCK:
        _WARMUP["ts"] = 0.0
        _WARMUP["running"] = False
