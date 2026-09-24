# -*- coding: utf-8 -*-
"""行情模块全接口压力测试（渐进式并发）"""
import sys
import os
import time
import json
import statistics
import threading
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, r"D:\xtquant")

BASE = "http://127.0.0.1:5000"
pc = time.perf_counter

# ── 测试股票代码（覆盖沪深主板/创业板/科创板/可转债） ──
TEST_CODES = [
    "000001",  # 平安银行
    "600519",  # 贵州茅台
    "000858",  # 五粮液
    "601318",  # 中国平安
    "000333",  # 美的集团
    "002475",  # 立讯精密
    "300750",  # 宁德时代
    "688981",  # 中芯国际
    "113050",  # 南银转债
    "127045",  # 鼎盛转债
]

# ── 要压测的接口（路径模板, 描述, 额外参数） ──
ENDPOINTS = [
    ("/market/quote/{code}",   "实时行情",  ""),
    ("/market/kline/{code}",   "日K线",     "count=120&period=1d"),
    ("/market/minute/{code}",  "分时数据",  ""),
    ("/market/depth/{code}",   "五档盘口",  ""),
    ("/market/tick/{code}",    "逐笔成交",  "count=40"),
]

CONCURRENCY_LEVELS = [10, 20, 50, 100]
ROUND_DURATION = 15  # 每轮持续秒数


# ──────────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────────
def http_get(url, timeout=30):
    """发起 HTTP GET，返回 (耗时秒, 状态码, 响应字节数, 错误信息)"""
    t0 = pc()
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        return pc() - t0, r.status, len(data), None
    except urllib.error.HTTPError as e:
        return pc() - t0, e.code, 0, str(e)
    except Exception as e:
        return pc() - t0, 0, 0, str(e)


def wait_for_server(url, timeout=60):
    """等待服务就绪"""
    t0 = pc()
    while pc() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def check_server_running():
    """检测服务是否已在运行"""
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def start_flask_server():
    """后台启动 Flask 服务"""
    import subprocess
    proc = subprocess.Popen(
        [sys.executable, r"D:\xtquant\app.py"],
        cwd=r"D:\xtquant",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc


# ──────────────────────────────────────────────────────────────────
# 压测引擎
# ──────────────────────────────────────────────────────────────────
def run_load_test(endpoint_tpl, desc, extra_params, concurrency, duration):
    """对单个接口进行固定并发、固定时长的压测。

    返回 dict:
      endpoint, desc, concurrency, duration,
      total_requests, success_count, error_count,
      latencies, p50, p90, p95, p99, avg, min, max, rps, avg_bytes
    """
    codes = TEST_CODES[:]
    latencies = []
    errors = 0
    successes = 0
    total_bytes = 0
    stop_time = pc() + duration
    lock = threading.Lock()

    def _worker():
        nonlocal errors, successes, total_bytes
        idx = 0
        while pc() < stop_time:
            code = codes[idx % len(codes)]
            idx += 1
            path = endpoint_tpl.format(code=code)
            sep = "&" if "?" in path else "?"
            url = f"{BASE}{path}"
            if extra_params:
                url += f"{sep}{extra_params}"

            elapsed, status, nbytes, err = http_get(url, timeout=30)
            with lock:
                if err or status >= 400:
                    errors += 1
                else:
                    successes += 1
                    total_bytes += nbytes
                    latencies.append(elapsed)

    workers = min(concurrency, len(codes) * 2)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_worker) for _ in range(concurrency)]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception:
                pass

    latencies.sort()
    n = len(latencies)
    result = {
        "endpoint": endpoint_tpl,
        "desc": desc,
        "concurrency": concurrency,
        "duration": duration,
        "total_requests": successes + errors,
        "success_count": successes,
        "error_count": errors,
        "p50": latencies[n // 2] if n else 0,
        "p90": latencies[int(n * 0.9)] if n else 0,
        "p95": latencies[int(n * 0.95)] if n else 0,
        "p99": latencies[int(n * 0.99)] if n else 0,
        "avg": statistics.mean(latencies) if latencies else 0,
        "min": latencies[0] if latencies else 0,
        "max": latencies[-1] if latencies else 0,
        "rps": successes / duration if duration else 0,
        "avg_bytes": total_bytes / successes if successes else 0,
    }
    return result


# ──────────────────────────────────────────────────────────────────
# 报告输出
# ──────────────────────────────────────────────────────────────────
def print_header():
    print("=" * 90)
    print("  行情模块全接口压力测试")
    print("  时间: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("  目标: %s" % BASE)
    print("  接口: %d 个  |  测试代码: %d 只  |  每轮持续: %ds" % (
        len(ENDPOINTS), len(TEST_CODES), ROUND_DURATION))
    print("  并发梯度: %s" % CONCURRENCY_LEVELS)
    print("=" * 90)


def print_round_result(r):
    ok_rate = (r["success_count"] / r["total_requests"] * 100) if r["total_requests"] else 0
    print("  %-6s  %-12s  并发=%-3d  请求=%-5d  成功=%-5d  失败=%-3d  "
          "RPS=%-7.1f  avg=%-6.1fms  p50=%-6.1f  p90=%-6.1f  p95=%-6.1f  p99=%-6.1f  %.0fKB" % (
        r["desc"][:6], r["endpoint"].split("/")[2],
        r["concurrency"], r["total_requests"], r["success_count"], r["error_count"],
        r["rps"], r["avg"] * 1000, r["p50"] * 1000, r["p90"] * 1000,
        r["p95"] * 1000, r["p99"] * 1000, r["avg_bytes"] / 1024))


def print_summary_table(all_results):
    """按接口汇总：最优并发、最大 RPS、p50 最低延迟"""
    print("\n" + "=" * 90)
    print("  汇总报告")
    print("=" * 90)
    by_ep = {}
    for r in all_results:
        key = r["endpoint"]
        by_ep.setdefault(key, []).append(r)

    print("  %-14s  %-6s  %-8s  %-8s  %-8s  %-8s" % (
        "接口", "描述", "最大RPS", "最优并发", "最优p50(ms)", "错误率"))
    print("  " + "-" * 76)
    for ep, rows in by_ep.items():
        best_rps = max(rows, key=lambda x: x["rps"])
        best_p50 = min(rows, key=lambda x: x["p50"] if x["error_count"] == 0 else 999)
        total_err = sum(r["error_count"] for r in rows)
        total_req = sum(r["total_requests"] for r in rows)
        err_pct = (total_err / total_req * 100) if total_req else 0
        desc = rows[0]["desc"]
        print("  %-14s  %-6s  %-8.1f  %-8d  %-8.1f  %-8.2f%%" % (
            ep.split("/")[2], desc, best_rps["rps"], best_rps["concurrency"],
            best_p50["p50"] * 1000, err_pct))


# ──────────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────────
def main():
    print_header()

    # 1) 检查/启动服务
    if check_server_running():
        print("\n[OK] Flask 服务已在运行")
    else:
        print("\n[启动] Flask 服务未检测到，正在后台启动...")
        proc = start_flask_server()
        print("  等待服务就绪 (PID=%d)..." % proc.pid)
        if not wait_for_server(f"{BASE}/health", timeout=90):
            print("[FAIL] 服务启动超时，请手动检查")
            sys.exit(1)
        print("[OK] 服务已就绪")

    # 2) 预热（单次请求让缓存生效）
    print("\n[预热] 发送预热请求...")
    for ep_tpl, desc, extra in ENDPOINTS:
        code = TEST_CODES[0]
        path = ep_tpl.format(code=code)
        sep = "&" if "?" in path else "?"
        url = f"{BASE}{path}"
        if extra:
            url += f"{sep}{extra}"
        http_get(url, timeout=30)
    print("[OK] 预热完成\n")

    # 3) 渐进式压测
    all_results = []
    for level in CONCURRENCY_LEVELS:
        print("-" * 90)
        print("  >> 并发=%d  每接口持续 %ds" % (level, ROUND_DURATION))
        print("-" * 90)
        for ep_tpl, desc, extra in ENDPOINTS:
            r = run_load_test(ep_tpl, desc, extra, level, ROUND_DURATION)
            all_results.append(r)
            print_round_result(r)
        print()

    # 4) 汇总
    print_summary_table(all_results)

    # 5) JSON 输出
    out_path = os.path.join(r"D:\xtquant", "stress_test_result.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print("\n[OK] 详细结果已保存: %s" % out_path)
    print("=" * 90)


if __name__ == "__main__":
    main()
