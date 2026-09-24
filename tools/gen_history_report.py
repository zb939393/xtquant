# -*- coding: utf-8 -*-
"""
从四轮"标准行情服务器池历史深度扫描"的 JSON 结果生成一份自包含 HTML 报告。

用法:
    python tools/gen_history_report.py
报告输出: docs/stock_history_scan_report.html
"""
import json
import os
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "docs", "stock_history_scan_report.html")

# (文件名, 周期标签, cat, 颜色)
PERIODS = [
    ("pool_scan_stock_FULL_600000SH.json", "1 分钟", 7, "#e4572e"),
    ("pool_scan_stock_cat0_FULL_600000SH.json", "5 分钟", 0, "#2e86ab"),
    ("pool_scan_stock_cat1_FULL_600000SH.json", "15 分钟", 1, "#17a398"),
    ("pool_scan_stock_cat2_FULL_600000SH.json", "30 分钟", 2, "#7b2cbf"),
]

FAIL_REASON = {
    "no-data@start0": "无数据",
    "TCP_UNREACHABLE": "TCP 不可达",
    "boundary-empty": "边界空(瞬断误判)",
}


def load(fn):
    with open(os.path.join(DATA, fn), "r", encoding="utf-8") as f:
        return json.load(f)


def top_n(results, n=12):
    ok = [r for r in results if r.get("ok")]
    ok.sort(key=lambda r: r["total_bars"], reverse=True)
    return ok[:n]


def fails(results):
    return [r for r in results if not r.get("ok")]


def svg_bars(items, value_fn, label_fn, fmt, color, unit, height=220):
    """通用横向柱状图 (SVG)。items: [(label, value)] 已按值降序。"""
    if not items:
        return "<p>（无数据）</p>"
    maxv = max(v for _, v in items) or 1
    row_h = 26
    w = 560
    bar_max = w - 200  # 留给标签
    h = len(items) * row_h + 20
    parts = [
        '<svg viewBox="0 0 %d %d" width="100%%" preserveAspectRatio="xMinYMin meet" '
        'style="font-family:inherit;font-size:12px">' % (w, h)
    ]
    for i, (label, v) in enumerate(items):
        y = 10 + i * row_h
        bw = max(2, int(v / maxv * bar_max))
        parts.append('<rect x="180" y="%d" width="%d" height="%d" rx="3" fill="%s"/>'
                     % (y, bw, row_h - 8, color))
        parts.append('<text x="176" y="%d" text-anchor="end" dominant-baseline="middle" fill="#333">%s</text>'
                     % (y + (row_h - 8) / 2, label))
        parts.append('<text x="%d" y="%d" dominant-baseline="middle" fill="#333" font-weight="600">%s</text>'
                     % (180 + bw + 6, y + (row_h - 8) / 2, fmt(v)))
    parts.append('</svg>')
    return "".join(parts)


def svg_compare(periods_data, height=200):
    """四周期对比：每周期一个分组(根数 / 天数)。"""
    labels = [p[1] for p in periods_data]
    bars = [p[3] for p in periods_data]   # total_bars
    spans = [p[4] for p in periods_data]  # span_days
    n = len(labels)
    w = 640
    group_w = w / n
    max_bars = max(bars) or 1
    max_span = max(spans) or 1
    chart_h = height
    parts = ['<svg viewBox="0 0 %d %d" width="100%%" preserveAspectRatio="xMinYMin meet" '
             'style="font-family:inherit;font-size:12px">' % (w, chart_h + 40)]
    # 两层：上=根数(蓝)，下=天数(橙)
    base_bars = 8
    base_span = 8 + chart_h / 2 + 18
    for i, (lab, b, s) in enumerate(zip(labels, bars, spans)):
        cx = group_w * i + group_w / 2
        # 根数柱（上）
        hb = int(b / max_bars * (chart_h / 2 - 10))
        parts.append('<rect x="%d" y="%d" width="%d" height="%d" rx="3" fill="#2e86ab"/>'
                     % (int(cx - 38), int(base_bars + (chart_h / 2 - 10) - hb), 36, hb))
        parts.append('<text x="%d" y="%d" text-anchor="middle" fill="#2e86ab" font-weight="600">%sk</text>'
                     % (int(cx - 20), int(base_bars + (chart_h / 2 - 10) - hb - 4), b // 1000))
        # 天数柱（下）
        hs = int(s / max_span * (chart_h / 2 - 10))
        parts.append('<rect x="%d" y="%d" width="%d" height="%d" rx="3" fill="#e4572e"/>'
                     % (int(cx + 2), int(base_span + (chart_h / 2 - 10) - hs), 36, hs))
        parts.append('<text x="%d" y="%d" text-anchor="middle" fill="#e4572e" font-weight="600">%d</text>'
                     % (int(cx + 20), int(base_span + (chart_h / 2 - 10) - hs - 4), s))
        parts.append('<text x="%d" y="%d" text-anchor="middle" fill="#333" font-weight="600">%s</text>'
                     % (int(cx), chart_h + 34, lab))
    parts.append('<text x="6" y="%d" fill="#2e86ab" font-size="11">■ 根数(千)</text>' % 4)
    parts.append('<text x="80" y="%d" fill="#e4572e" font-size="11">■ 日历天数</text>' % 4)
    parts.append('</svg>')
    return "".join(parts)


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def main():
    loaded = []
    for fn, label, cat, color in PERIODS:
        d = load(fn)
        res = d["results"]
        ok_res = [r for r in res if r.get("ok")]
        deep = ok_res and max(ok_res, key=lambda r: r["total_bars"])
        loaded.append({
            "fn": fn, "label": label, "cat": cat, "color": color,
            "meta": d, "results": res, "ok": ok_res, "deep": deep,
            "n_ok": len(ok_res), "n_fail": len(res) - len(ok_res),
        })

    # ---- HTML 构建 ----
    sections = []
    # 核心对比表
    rows = ""
    for p in loaded:
        d = p["deep"]
        rows += ("<tr><td><span class='dot' style='background:%s'></span>%s</td>"
                 "<td>%d</td><td class='num'>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>\n" % (
            p["color"], p["label"], p["cat"],
            f'{d["total_bars"]:,}', d["earliest"][:10], d["latest"][:10],
            f'{d["span_days"]:,} 天'))
    # 对比图
    compare_svg = svg_compare([("", p["label"], p["cat"], p["deep"]["total_bars"], p["deep"]["span_days"])
                               for p in loaded])

    # 各周期详情块
    detail_html = ""
    for p in loaded:
        deep = p["deep"]
        t10 = top_n(p["results"], 12)
        top_rows = ""
        for i, r in enumerate(t10, 1):
            top_rows += ("<tr><td>%d</td><td>%s</td><td class='mono'>%s:%d</td>"
                         "<td class='num'>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>\n" % (
                i, esc(r["name"]), r["ip"], r["port"],
                f'{r["total_bars"]:,}', r["earliest"][:10], r["latest"][:10], r["span_days"]))
        fl = fails(p["results"])
        fail_rows = ""
        for r in fl:
            reason = FAIL_REASON.get(r.get("reason", ""), r.get("reason", "未知"))
            fail_rows += "<tr><td>%s</td><td class='mono'>%s:%d</td><td>%s</td></tr>\n" % (
                esc(r.get("name", "?")), r.get("ip", "?"), r.get("port", 0), reason)
        if not fail_rows:
            fail_rows = "<tr><td colspan='3' class='muted'>无</td></tr>"

        chart = svg_bars(
            [(r["name"], r["total_bars"]) for r in t10],
            None, None, lambda v: f"{v:,}", p["color"], "根")

        detail_html += f"""
        <div class="card">
          <h3>{esc(p['label'])} <span class="tag">cat={p['cat']}</span></h3>
          <div class="stat-row">
            <div class="stat"><div class="k">全池最深根数</div><div class="v">{deep['total_bars']:,}</div></div>
            <div class="stat"><div class="k">最早日期</div><div class="v">{deep['earliest'][:10]}</div></div>
            <div class="stat"><div class="k">日历跨度</div><div class="v">{deep['span_days']} 天</div></div>
            <div class="stat"><div class="k">可达且有数据</div><div class="v">{p['n_ok']} / {p['meta']['scanned']}</div></div>
          </div>
          <p class="anchor-line">🏆 最大历史服务器：<b>{esc(deep['name'])}</b>
             (<span class="mono">{deep['ip']}:{deep['port']}</span>)</p>
          <div class="chart">{chart}</div>
          <h4>Top 12 排名</h4>
          <table>
            <thead><tr><th>#</th><th>服务器</th><th>地址</th><th>根数</th><th>最早</th><th>最晚</th><th>天数</th></tr></thead>
            <tbody>{top_rows}</tbody>
          </table>
          <h4>不可达 / 无数据（{p['n_fail']} 台）</h4>
          <table class="fail">
            <thead><tr><th>服务器</th><th>地址</th><th>原因</th></tr></thead>
            <tbody>{fail_rows}</tbody>
          </table>
        </div>
        """

    ts0 = loaded[0]["meta"]["ts"]
    code = loaded[0]["meta"]["code"]
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A股标准行情池 · 历史K线深度扫描报告</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  margin: 0; background: #f5f7fa; color: #1f2937; line-height: 1.6; }}
.wrap {{ max-width: 980px; margin: 0 auto; padding: 28px 20px 60px; }}
header {{ background: linear-gradient(135deg,#2e86ab,#17a398); color:#fff; padding: 26px 28px; border-radius: 14px; }}
header h1 {{ margin:0 0 6px; font-size: 24px; }}
header .sub {{ opacity:.92; font-size: 13px; }}
h2 {{ margin-top: 38px; padding-left: 11px; border-left: 5px solid #2e86ab; font-size: 19px; }}
h3 {{ margin: 22px 0 12px; font-size: 16px; }}
h4 {{ margin: 18px 0 8px; font-size: 14px; color:#374151; }}
.card {{ background:#fff; border:1px solid #e5e7eb; border-radius:12px; padding:18px 20px; margin:14px 0;
  box-shadow: 0 1px 3px rgba(0,0,0,.05); }}
table {{ width:100%; border-collapse: collapse; font-size: 13px; margin: 8px 0; }}
th,td {{ text-align:left; padding: 7px 9px; border-bottom:1px solid #eef0f3; }}
th {{ background:#f8fafc; color:#475569; font-weight:600; }}
td.num {{ text-align:right; font-variant-numeric: tabular-nums; }}
td.mono, .mono {{ font-family: ui-monospace, Menlo, Consolas, monospace; font-size:12.5px; }}
.muted {{ color:#9ca3af; }}
.dot {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:7px; vertical-align:middle; }}
.tag {{ background:#eef2f7; color:#475569; font-size:11px; padding:2px 8px; border-radius:20px; margin-left:6px; }}
.stat-row {{ display:flex; flex-wrap:wrap; gap:12px; margin:12px 0; }}
.stat {{ background:#f8fafc; border:1px solid #eef0f3; border-radius:10px; padding:10px 14px; min-width:120px; }}
.stat .k {{ font-size:11px; color:#6b7280; }}
.stat .v {{ font-size:19px; font-weight:700; color:#111827; }}
.anchor-line {{ background:#fff7ed; border:1px solid #fed7aa; color:#9a3412; padding:9px 12px;
  border-radius:9px; font-size:13.5px; }}
.chart {{ background:#fcfcfd; border:1px solid #eef0f3; border-radius:10px; padding:10px; margin:10px 0; }}
table.fail th {{ background:#fef2f2; }}
.callout {{ background:#ecfdf5; border:1px solid #a7f3d0; border-radius:12px; padding:14px 18px; margin:16px 0; }}
.callout b {{ color:#065f46; }}
.note {{ font-size:12.5px; color:#6b7280; }}
.foot {{ margin-top:34px; font-size:12px; color:#9ca3af; text-align:center; }}
ul {{ padding-left: 20px; }}
li {{ margin: 4px 0; }}
code {{ background:#eef2f7; padding:1px 6px; border-radius:5px; font-size:12.5px; }}
</style></head>
<body><div class="wrap">

<header>
  <h1>A股标准行情服务器池 · 历史 K 线深度扫描报告</h1>
  <div class="sub">样本股 {code} ｜ 扫描日期 {ts0} ｜ 109 台标准行情服务器（TdxHq，端口 7709）｜
  报告生成 {now}</div>
</header>

<h2>一、背景与目标</h2>
<div class="card">
<p>为回答「<b>股票最大历史数据能否通过服务器方法读到 1/5 分钟 K 线</b>」，本项目沿用 VWAP 期指深历史的
「<b>服务器池 + 分页取数</b>」思路，编写 <code>tools/scan_pool_history.py</code> 遍历 A 股标准行情池，
逐台用二分查找探测各周期 K 线的最旧端，排序找出<b>保留历史最深的服务器</b>，作为 A 股历史数据的「锚点」。</p>
<p class="note">关键纠正：股票历史 K 线在<b>标准行情池（TdxHq）</b>，不是期指专用的扩展行情池（ExHq）。
二者服务器清单、端口、取数接口均不同。</p>
</div>

<h2>二、方法论</h2>
<div class="card">
<ul>
  <li><b>取数后端</b>：<code>pytdx.hq.TdxHq_API.get_security_bars(cat, market, code6, start, count)</code>，端口 7709。</li>
  <li><b>start 语义（实测定论）</b>：<code>start=0</code> 取<b>最新一页</b>（距今天最近），<code>start</code> 增大向历史回退，越过总量即返回空；
      每页<b>时间升序</b>（首根最旧、末根最新）；单次上限约 700 根。</li>
  <li><b>深度探测算法</b>：从最新页出发，倍增找上界 → 二分定位「最后一个整页 start（最旧端）」→
      该页首根即绝对最旧 bar，总量 = start + 该页根数。带 TCP 可达性预检 + 取数重试，避免把瞬断误判成历史末端。</li>
  <li><b>周期类别</b>：<code>_PERIOD_CAT</code> 映射 —— 1min=7、5min=0、15min=1、30min=2。</li>
  <li><b>扫描范围</b>：标准行情池全量 109 台，12 并发，单台超时 6s。</li>
</ul>
</div>

<h2>三、核心发现：四周期横向对比</h2>
<div class="card">
<table>
  <thead><tr><th>周期</th><th>cat</th><th>全池最深根数</th><th>最早日期</th><th>最晚日期</th><th>日历跨度</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
<div class="chart">{compare_svg}</div>
<div class="callout">
  <b>⚠️ 推翻了「根数上限固定、周期越粗回溯越久」的初步假设。</b><br>
  实测表明标准行情主站对<b>子日线（分钟级）统一保留约 2 年的日历深度</b>，而非固定根数上限：
  <ul style="margin:8px 0 0">
    <li><b>1 分钟</b>：单根太密，24000 根只够 ~141 天就先撞上根数天花板 → <b>反而最短</b>；</li>
    <li><b>5 分钟</b>：24000 根 ≈ 刚好铺满 2 年 → <b>达到「最深日历 2 年」且根数最大</b>；</li>
    <li><b>15 / 30 分钟</b>：在 2 年窗口内根数分别降到 8000 / 4000，但<b>日历深度一样卡在 2024-09-03，再粗也穿不过 2 年</b>。</li>
  </ul>
  <p style="margin:8px 0 0"><b>结论</b>：bar 数随周期线性递减，<b>日历深度在 5 分钟已封顶于 ~2 年</b>。
  想拿 A 股标准主站能给的最长历史，<b>5 分钟（cat=0）就是最优解</b>——既最长（2 年）又最密（24000 根）。</p>
</div>
</div>

<h2>四、各周期详情（全量 109 台）</h2>
{detail_html}

<h2>五、最优策略与锚点配置</h2>
<div class="card">
<ul>
  <li><b>最长历史锚点</b>：<code>国君南京电信 103.221.142.65:7709</code>（同机房 .66~.70 / .72 共 7 台，每个周期都拿全池最深，冗余充足）。</li>
  <li><b>周期选择</b>：要最长历史用 <b>5 分钟（cat=0）</b>；要更高分辨率可用 1 分钟，但其日历仅 ~141 天；15/30 分钟不会更长。</li>
  <li><b>落地建议</b>：参照 <code>tools/backfill_vwap_history.py</code> 框架编写
      <code>backfill_stock_history.py</code>，以国君南京电信为锚点，按代码分批拉 5 分钟 K 线落盘
      <code>data/stock_hist/</code>，带增量 delta + 断点续传 + 并发。</li>
  <li><b>局限</b>：标准主站对任意分钟线最深仅 ~2 年；要 2 年以上 A 股历史须另寻数据商或 akshare 后复权。</li>
</ul>
</div>

<h2>六、可复用工具</h2>
<div class="card">
<p><code>tools/scan_pool_history.py</code> 已支持任意周期（<code>--cat</code>）与任意股票（<code>--code</code>），
可随时重扫验证。常用命令：</p>
<pre style="background:#0f172a;color:#e2e8f0;padding:12px 14px;border-radius:9px;font-size:12.5px;overflow:auto"># 扫描某股票 5 分钟历史深度
python tools/scan_pool_history.py --code 600519.SH --cat 0 --out data/pool_scan_600519_5m.json

# 离线验证二分算法（不联网）
python tools/scan_pool_history.py --selftest</pre>
</div>

<div class="foot">本报告由四轮实连扫描（1/5/15/30 分钟，共 436 台次探测）自动汇总生成 ｜ 数据见
<code>data/pool_scan_stock_*.json</code></div>

</div></body></html>"""

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print("报告已生成:", OUT)


if __name__ == "__main__":
    main()
