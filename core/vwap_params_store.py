# -*- coding: utf-8 -*-
"""VWAP 策略参数的跨窗口共享存储（服务端持久化）。

背景：期指看板（/futures/board）上调好的 dev_z 阈值（k_long / k_short / rr / days）
需要被这些地方看到，且口径必须一致：
    - /futures/popup        期指综合看盘 · 独立窗口（pywebview）
    - /futures/popup/integrated  市场概况综合窗口（iframe 嵌入 /futures/popup）
    - 主页面「分时看盘」副图的 dev_z 阈值线
独立窗口与主页面未必共享 localStorage（pywebview 与浏览器 storage 分区可能不同），
故以服务端文件为准，localStorage 只作为前端即时生效的补充。

存储：data/vwap_params.json -> {"IFL9": {"k_long": 1.05, "k_short": -1.35, "rr": 1.5, "days": 30}, ...}
"""
import os
import json
import threading
import tempfile

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_BASE, "data", "vwap_params.json")

_LOCK = threading.Lock()
DEFAULTS = {"k_long": 1.0, "k_short": -1.5, "rr": 1.5, "days": 30}
FIELDS = ("k_long", "k_short", "rr", "days")


def _read():
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return {}


def _write(obj):
    d = os.path.dirname(_PATH)
    if d and not os.path.isdir(d):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            return False
    try:
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".vwap_params.", suffix=".tmp")
    except Exception:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _PATH)
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False


def get_all():
    """返回全部品种的参数 dict（原样）。"""
    with _LOCK:
        return _read()


def get(code):
    """取某品种参数（只含数值字段，缺失项不返回）。"""
    if not code:
        return {}
    with _LOCK:
        obj = _read().get(code) or {}
    out = {}
    for k in FIELDS:
        v = obj.get(k)
        if v is None:
            continue
        try:
            out[k] = float(v)
        except Exception:
            continue
    return out


def set_params(code, params):
    """写入（按字段增量更新），返回合并后的完整参数。"""
    if not code:
        return {}
    clean = {}
    for k in FIELDS:
        v = (params or {}).get(k)
        if v is None:
            continue
        try:
            clean[k] = float(v)
        except Exception:
            continue
    with _LOCK:
        obj = _read()
        merged = dict(obj.get(code) or {})
        merged.update(clean)
        for k in FIELDS:
            if k not in merged:
                merged[k] = DEFAULTS[k]
        obj[code] = merged
        _write(obj)
        return dict(merged)
