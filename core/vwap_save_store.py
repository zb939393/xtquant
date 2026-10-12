# -*- coding: utf-8 -*-
"""VWAP 策略「参数 + 绩效成绩」快照的持久化存储。

背景：期指看板 VWAP 模式下调好的各品种阈值（k_long / k_short / rr / days / am_k / am_rr）
配合实时绩效（当日 R、信号次数、ret1545、dev_z…）构成一次「调参 + 成绩」记录，
用户点击「保存」后落盘，便于日后回看 / 比对不同参数组合的成绩。

存储：data/vwap_saved_runs.json -> [ {id, ts, ts_unix, note, params, perf}, ... ]
（按时间正序追加；前端取回后倒序展示，最新在前）
"""
import os
import json
import time
import threading
import tempfile
import uuid

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_BASE, "data", "vwap_saved_runs.json")

_LOCK = threading.Lock()
MAX_RUNS = 500


def _read():
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if isinstance(obj, list):
            return obj
    except Exception:
        pass
    return []


def _write(obj):
    d = os.path.dirname(_PATH)
    if d and not os.path.isdir(d):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            return False
    try:
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".vwap_saved_runs.", suffix=".tmp")
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


def append_run(note, params, perf):
    """追加一条快照，返回完整记录 dict（含 id / ts）。"""
    if not isinstance(params, dict):
        params = {}
    if not isinstance(perf, dict):
        perf = {}
    note = note or ""
    if not isinstance(note, str):
        try:
            note = str(note)
        except Exception:
            note = ""
    ts_unix = int(time.time())
    rid = time.strftime("%Y%m%d_%H%M%S", time.localtime(ts_unix)) + "_" + uuid.uuid4().hex[:6]
    rec = {
        "id": rid,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_unix)),
        "ts_unix": ts_unix,
        "note": note,
        "params": params,
        "perf": perf,
    }
    with _LOCK:
        lst = _read()
        lst.append(rec)
        if len(lst) > MAX_RUNS:
            lst = lst[-MAX_RUNS:]
        _write(lst)
        return dict(rec)


def list_runs():
    """返回全部快照（时间正序）。"""
    with _LOCK:
        return _read()


def delete_run(rid):
    """按 id 删除一条；返回是否删除成功。"""
    if not rid:
        return False
    with _LOCK:
        lst = _read()
        nv = [r for r in lst if (r.get("id") != rid)]
        if len(nv) == len(lst):
            return False
        _write(nv)
        return True
