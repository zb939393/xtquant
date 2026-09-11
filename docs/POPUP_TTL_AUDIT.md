# 弹窗/页面「缓存寿命 ttl vs 轮询间隔」全量体检

生成时间：2026-09-11
扫描范围：`templates/*.html`（17 个，排除 `.bak`）+ `api/*.py`（路由默认 ttl）+ `core/ak_service.py`（函数签名默认值）
判据：**服务端缓存 `ttl` 应 ≤ 前端轮询间隔**。`ttl > 间隔` 意味着「部分轮询打空、返回上一轮旧值」，数据更新频率被缓存拉到 `ttl`。

---

## 一、结论速览

| 判定 | 数量 | 说明 |
|---|---|---|
| ✅ 对齐 | 9 | ttl ≤ 间隔，每次（或大多数）轮询都能取到新数据 |
| ❌ 错配（真 bug） | 0 | ~~市场概况~~ **已修**（2026-09-11，详见缺陷 1） |
| ⚪ 刻意节流 | 3 | ttl > 间隔，但下游数据本身是分钟/5 分钟粒度，属有意为之 |
| ⚠️ 写死 ttl（刷新失效风险） | 2 | 剩余项均为**主页面节流**、不是刷新入口：`index.html` / `option.html` 的分时（10）。弹窗侧已全部 force 化或加按钮（缺陷 1/2/3/5） |
| 🔌 无缓存概念 | 3 | JSONP + `no-store` / 图片 `_random`，天然每次必新；`external` 的自动刷新已由 5s 定频改为**分钟对齐**（缺陷 4） |

---

## 二、弹窗对照表（核心）

| 窗口 | 数据接口 | 后端默认 | 前端实际 ttl | 轮询间隔 | 判定 |
|---|---|---|---|---|---|
| **amountflow_popup** 两市成交 | `/futures/amountflow/data` | 60 | `force?0:60` | 60s | ✅ 对齐（本次修） |
| **zhangfu_popup** 涨幅分布 | `/futures/zhangfu/distribution` | 30 | `force?0:30` | 30s | ✅ 对齐 |
| **industry_popup** 行业分布 | `/futures/popup/industry` | 30 | `force?0:30` | 30s | ✅ 对齐 |
| **market_overview** 市场概况 | `/futures/popup/overview` | 30 | `force?0:30` | 30s | ✅ **已修**（原 ttl 60 写死） |
| **news_popup** 新闻快讯 | `/futures/news/data` | 20 | `force?0:20` | 60s | ✅ 对齐／**已加刷新按钮**（缺陷 5；页面路由 `/futures/news/popup`） |
| **industry_stocks_popup** 个股分布 | `/futures/industry/stocks/data` | 15 | `force?0:15` | 30s | ✅ 对齐／**已加刷新按钮**（缺陷 5） |
| **stock_chart_popup** 个股分时 | `/market/depth`·`/market/tick`·`/market/minute`·`/market/kline` | 3·3·10·120 | `force?0:3`·`force?0:3`·`force?0:10`·`force?0:30` | 30s | ✅ 对齐／**已加刷新按钮**（缺陷 5；K线 ttl 原路由不透传，已修） |
| **futures_popup** 期指综合看盘·格子分时 | `/futures/exquote/minute` | 3 | `force?0:3` | 3s | ✅ 对齐 |
| **futures_popup** 期指综合看盘·格子K线 | `/futures/exquote/kline` | 15 | `force?0:15` | 3s | ⚪ 刻意节流（K线 5min/30min 粒度） |
| **futures_popup** 根实例快照 | `/futures/snapshot` | 无缓存 | — | 5s | ✅ 无缓存 |
| **option_watchlist_popup** 期权自选·列表 | `/option/full?force=1` | 6（顶层） | `force?0:默认` | 5s | ✅ 对齐（`force=1` 有效） |
| **option_watchlist_popup** 期权自选·格子 | `/option/exquote/minute`·`quote`·`tick` | 10·3·3 | `force?0:10/3/3` | 3s | ⚪ 分时节流／✅ 刷新已透传（缺陷 3 已修） |
| **capital_popup** 股指资金 | `/futures/capital/fflow`·`sina`（JSONP） | 无缓存（`no-store`） | 无 ttl | 20s（仅 9:20–11:35 / 12:55–15:10） | ✅ 无缓存概念 |
| **external_popup** 外围股市 | `/futures/external/img?nid=` | 无缓存 | 无 ttl（`&_random=`） | **分钟对齐**（边界+2s） | ✅ 无缓存／已改为分钟对齐（缺陷 4，原 5s） |
| **integrated_popup** 综合多面板 | iframe 容器，自身不取数 | — | — | — | ✅ ~~见缺陷 2~~ **已修** |

---

## 三、主页面（非弹窗，一并扫）

| 页面 | 接口 | ttl | 间隔 | 判定 |
|---|---|---|---|---|
| **futures.html** | `/futures/snapshot` | 无缓存 | `refreshInterval` 默认 5s（可下拉调） | ✅ |
| | `/futures/exquote/minute` | 3 | 3s | ✅ |
| | `/futures/exquote/kline` | 15 | 3s | ⚪ K线粒度决定，合理 |
| | `/futures/exquote/quote`·`tick` | 3·3 | 3s | ✅ |
| **index.html** | `/market/minute` | 10 | 3s | ⚪ 分时按分钟粒度，合理 |
| | `/market/depth`·`tick` | 3·3 | 3s | ✅ |
| | 自选实时（`code_in` 增量） | — | `refreshInterval` | ✅ |
| **option.html** | `/option/exquote/minute` | 10 | 3s | ⚪ 同上 |
| | `/option/exquote/quote`·`tick` | 3·3 | 3s | ✅ |
| | 全量列表 | 6（顶层） | `refreshInterval` 默认 5s | ✅ |

> 分时（`ttl=10` / 轮询 3s）与 K线（`ttl=15` / 轮询 3s）**不是 bug**：分时数据按分钟更新、K线是 5min/30min 周期，10s 内重复取没有新信息。ttl 在这里是**有意的下游节流**，同时把轮询的响应打到 0.01s 级。

---

## 四、缺陷清单（按优先级）

### 缺陷 1（真 bug）市场概况 `market_overview.html` —— ✅ 已修（2026-09-11）
> 页面路由是 `/overview`，同时也是**整个服务的首页 `/`**，所以这个错配影响的是默认落地页。
- **现象**：轮询 30s，但 URL 写死 `?ttl=60` → 每 2 次轮询只有 1 次真取数，**数据实际每 60s 才更新一次**。
  页面脚注还写着「每 30 秒自动刷新」，与实际不符。
- **并发缺陷**：有刷新按钮 `@click="fetchData(true)"`，但 `force` 被忽略（URL 写死 60）→ **点刷新打的是缓存**。
  与之前 `zhangfu` / `amountflow` 修掉的是同一类毛病。
- **已做的改动**（三处一起改）：
  | 位置 | 原值 | 新值 |
  |---|---|---|
  | `templates/market_overview.html:145` | 写死 `?ttl=60` | `const ttl = force ? 0 : 30;` |
  | `api/futures_bp.py:590` `/popup/overview` | `get("ttl","60")` | `get("ttl","30")` |
  | `core/ak_service.py:2737` | `def get_market_overview(ttl=60)` | `ttl=30`（docstring 同步改） |
- **验证**：后端不传 ttl 连取两次 **0.96s → 0.00s**（真取数→命中缓存）；`ttl=0` **0.88s** vs `ttl=30` **0.03s**；
  页面（`/overview` 与 `/`）内 `const ttl = force ? 0 : 30`、URL 用 ttl 变量、无 `ttl=60` 残留；
  回归测试 `tests/test_popup_refresh_market_overview.js` **28 项断言全 PASS**（含"刷新后再轮询 ttl 自动回到 30"，
  证明是每次现算、无需复位）。服务已重启（`core/` 改动），pid 29468 → 7376。

### 缺陷 2（体验 bug）`integrated_popup.html`「刷新全部」—— ✅ **已修（2026-09-11）**
- **现象**：它是 **iframe 容器**，「刷新全部」的实现是重挂子页 `iframe.src = base + '?_t=' + Date.now()`。
  子页重新加载后走的是**首屏 `fetchData(false)`** → 用各自默认 ttl → **可能直接命中服务端缓存**。
- **后果**：`_t=` 只绕过了浏览器缓存；`amountflow`(60s) / `zhangfu`(30s) / `industry`(30s) / `news`(20s)
  这几个面板「看起来刷新了、画的还是旧数据」。反倒是 `capital`（no-store）和 `external`（`_random`）真刷新。
- **修法（已实施）**：重挂时给子页 URL 追加 `&force=1`，各子页首屏读 `force=1` → `fetchData(true)`（ttl=0）。

| 文件 | 改动 |
|---|---|
| `integrated_popup.html` | `refreshAll()` 重挂 URL 改为 `base + '?_t=' + (Date.now()+i) + '&force=1'`；仍以 `split('?_t=')[0]` 剥离旧参数，连点不会累积。顺带把它的 vue 从 `unpkg` 换成本地 `/static/vue.min.js`（同版本）—— CDN 挂了容器工具栏（含「刷新全部」）根本渲染不出来 |
| `amountflow_popup.html` | 新增模块级 `FORCE_ON_LOAD`（读 `location.search`）→ `mounted` 里 `fetchData(FORCE_ON_LOAD)` |
| `zhangfu_popup.html` | 同上（默认 30s） |
| `industry_popup.html` | 同上（`created` 里那次首屏） |
| `news_popup.html` | `fetchData(force)` 的 force 原来**被忽略**（URL 写死 `ttl=20`）→ 改为 `ttl=' + (force ? 0 : 20)`；首屏保持 `fetchData(true)` 原意图 |
| `futures_popup.html` | 根实例 `mounted` 里 `if (FORCE_ON_LOAD) dispatch('__forcerefresh')` —— 快照无缓存但 16 个格子带 ttl=3/15，必须广播 |
| `option.html` | **无需改动**：首屏本就是 `fetchData(true)` → `force=1`；且默认 `boardMode=false`（列表视图，不渲染格子） |

- **为什么 futures/option 不用「首屏 force」而用广播**：它们的首屏请求分两层——根实例的快照/列表（本身已 force）
  和 16 个格子组件（各自 ttl=3/15）。格子是 `IntersectionObserver` 节流的，未进视野不能立刻取，
  只能广播让它记账（`_forcePending`）、回到视野再强取。option.html 列表模式无格子，故无需广播。
- **一个必须记住的顺序假设**：广播放在**根实例** `mounted` 里，依赖「Vue 2 中子组件 `mounted` 先于父组件 `mounted`」——
  否则事件发出去时格子还没注册监听。回归测试专门用「格子先 mounted、再父 mounted」的顺序复现了这一点。
- **验证**：`tests/test_popup_refresh_force_on_load.js` 65 项断言全 PASS（含"哪些入口 ttl=0 / 轮询不被 force 污染 /
  二次刷新不累积参数"）；HTTP 实测 `news ttl=0` 0.526s（真取数）vs `ttl=20` 0.013s（命中缓存）。

### 缺陷 3（功能缺口）期权自选格子的刷新不透传 —— ✅ 已修（2026-09-11）
- **现象**：`option_watchlist_popup` 的刷新按钮 `reload(true)` → `/option/full?force=1` **本来就有效**
  （后端 `_OPT_FULL_CACHE` 6s 缓存会同步刷新）；但每张卡片的 `opt-board-rt` 格子取的三个接口 ttl 写死
  `minute=10 / quote=3 / tick=3`，**不吃 force 参数** → 列表换了、格子里的分时和盘口还是旧的。
- **修法**（照 `futures_popup` 的成熟做法，机制同缺陷 2 的格子部分）：

| 位置 | 改动 |
|---|---|
| 组件 `opt-board-rt` | `mounted` 注册 / `beforeDestroy` 摘除 `window.addEventListener('__forcerefresh', this._onForce)` |
| 组件 `_onForce()` | 可见 → `this._refresh(true)`；不可见 → `this._forcePending = true` 记账 |
| 组件 `_setupIO().start()` | 首绘前消费记账：`const f = this._forcePending; this._forcePending = false; this._refresh(f);` |
| 组件 `_refresh/_refreshLive` | 加 `force` 参数并往下传（`_drawChart` / `_loadQuote` / `_loadTicks`） |
| 三个接口 URL | `?ttl=' + (force ? 0 : 10/3/3)`（原来是写死的 `ttl=10` / `ttl=3`） |
| 根实例 | 新增 `refreshAll()` = 广播 `__forcerefresh` + `reload(true)`；工具栏按钮由 `reload(true)` 改绑它 |
| 根实例 `mounted` | `?force=1` 时广播一次（综合窗口「刷新全部」重载本页的入口），滚出视野的格子靠记账兜住 |

- **顺手修掉的一处潜在失效**：组件里 `if(typeof IntersectionObserver === 'undefined'){ start(); return; }`
  这一支不会把 `_visible` 置真 → 广播只会记账、**永远没人消费**（刷新静默失效）。
  已改为 `{ this._visible = true; start(); return; }`。`futures_popup.html` 两个组件同样形状，已一并补上（各 1 行）。
- **验证**：`tests/test_popup_refresh_option_watchlist.js` **60 项断言全 PASS**（运行时计数；含"记账期间一个请求都不发 /
  回到视野首绘即 ttl=0 / 轮询回到 10·3·3 / 销毁后广播不再引发请求"）；
  六个回归测试合计 **357 项断言全 PASS**（65+53+28+60+63+88，2026-09-11 缺陷 5 完成后）。
  HTTP 实测 `minute` ttl=0 **0.131s** vs ttl=10 **0.023s**、`quote` **0.126s** vs **0.003s**、
  `tick` **0.082s** vs **0.019s**（均 200，条数一致）。

### 缺陷 4（配比）外围股市轮询过密 —— ✅ 已修（2026-09-11）
- **现象**：`external_popup` 每 **5s** 重挂全部 8 张外盘图 URL（`refreshAll`），等于每分钟重下 **12 轮整批图**。
  外盘图是分钟级数据（后端代理东财 `imageType=rt`），纯浪费；且每轮都把失败图的 `retry/failed` 清零，
  使「重试 3 次后置失败」的退避逻辑永远走不完。
- **修法**（选「分钟对齐」而非简单调间隔）：改为**对齐「下一分钟边界 + 2s 宽限」**的递归 `setTimeout`。
  ```js
  scheduleNextRefresh(){
    if(this.timer){ clearTimeout(this.timer); }
    var PERIOD = 60000, GRACE = 2000;
    var delay = PERIOD - (Date.now() % PERIOD) + GRACE;
    this.timer = setTimeout(() => {
      try { this.refreshAll(); } finally { this.scheduleNextRefresh(); }
    }, delay);
  }
  ```
  | 位置 | 原值 | 新值 |
  |---|---|---|
  | `created()` | `setInterval(() => this.refreshAll(), 5000)` | `this.scheduleNextRefresh()` |
  | `beforeDestroy()` | `clearInterval(this.timer)` | `clearTimeout(this.timer)`（递归 setTimeout 的句柄） |
  | `methods` | — | 新增 `scheduleNextRefresh()` 调度器 |
- **为什么用递归 setTimeout 而不是 `setInterval(…, 60000)`**：① 定频会被「首次触发时刻」带偏
  （14:23:17 开窗 → 此后每分钟 :17 才取数，而不是边界后立刻拿到最新一分钟）；② 手动刷新不触碰调度。
  两处都补了 `try/finally`：`refreshAll` 抛异常也不会让调度链断掉。
- **效果**：
  | | 请求量 | 流量（实测单张 3.0 KB） | 数据新鲜度 |
  |---|---|---|---|
  | 旧 5s 定频 | 96 次/分 | ≈ 289 KB/分（**16.9 MB/时**） | ≤5s（对分钟级数据是浪费） |
  | 新 分钟对齐 | **8 次/分** | ≈ **24 KB/分（1.4 MB/时）** | 边界后 +2s 即拿当分钟最新图 |
  → 请求降 **12 倍**，流量降约 **16.9 → 1.4 MB/时**。
- **验证**：`tests/test_popup_refresh_external.js` **63 项断言全 PASS**；HTTP 实测 8/8 张图仍可代理（200 / 3.0 KB / ~0.2s）。

### 缺陷 5（功能缺口）三个窗口没有刷新按钮 —— ✅ **已修**（2026-09-11，最后一批）
- **`news_popup`**（路由 `/futures/news/popup`）：原本**完全没有工具栏 HTML**（只剩 CSS 和遗留死代码 `toggleTop`）。
  补整套 `tb-hover + .toolbar`（置顶/刷新/关闭，`#refreshBtn` + `:disabled="loading"` + `.btn:disabled`）。
  `fetchData(force)` 的 force 已在缺陷 2 修复中 force 化（`force?0:20`），本次只接按钮 + `refresh()`。
- **`industry_stocks_popup`**（路由 `/futures/industry/stocks?board=&name=`）：有工具栏（置顶/关闭），加「刷新」按钮 + `refresh()`（含 `render()` 后 resize 兜底）；URL `&ttl=15` 写死 → `force ? 0 : 15`。
- **`stock_chart_popup`**（路由 `/futures/stock/chart?code=&market=&name=&pc=`）：加按钮 + `refresh()`；分时 `ttl=10`/盘口 `3`/逐笔 `3` 全部 force 化；**K线原来连 ttl 都不发**（依赖路由默认 120）→ 前端补 `ttl=' + (force ? 0 : 30) + '`，
  同时 `api/market_bp.py` 的 `/market/kline` **把 ttl 透传给 `get_kline_pytdx(ttl=120)`**（此前函数有 ttl 参数但路由从不传 → 前端无论发什么都按 120s 缓存）。
  `refresh()` 语义 = 分时/盘口/逐笔（若盘口开）一起 `ttl=0`；K线视图刷新也 `ttl=0`。
- **验证**：`tests/test_popup_refresh_defect5.js` **88 项运行时断言全 PASS**；六套合计 **357 项全 PASS**。
  HTTP 实测（重启后 pid=156）：`news ttl=0` **0.565s** vs `ttl=20` 0.015s；`istk ttl=0` **3.217s** vs 0.028s；
  `minute ttl=0` **0.331s** vs 0.013s；**`kline ttl=0` 0.290s（真取数，透传生效）** vs 默认 0.018s；`depth/tick ttl=0` 均 200。
  三页面 200 且按钮/`:disabled`/force 三元式/closeWin 全部就位。回归 12 路由全 200。

> **测试断言数按「运行时 PASS 行」计**（循环里的断言会被展开，比源文件里 `check(` 的静态条数多）。
> 改动后请重跑：`for t in tests/test_popup_refresh_*.js; do node "$t"; done` —— 当前 **6 个文件合计 357 项全 PASS**。
> （external 63 + force_on_load 65 + futures 53 + market_overview 28 + option_watchlist 60）。

---

## 五、附：后端路由默认 ttl 全表（`api/*.py`）

| 文件 | 路由 | 默认 ttl |
|---|---|---|
| futures_bp.py | `/exquote/minute/<code>` | 3 |
| futures_bp.py | `/exquote/quote/<code>` | 3 |
| futures_bp.py | `/exquote/tick/<code>` | 3 |
| futures_bp.py | `/exquote/kline/<code>` | 15 |
| futures_bp.py | `/vwap/<code>` | 3 |
| futures_bp.py | `/news/data` | 20 |
| futures_bp.py | `/popup/industry` | 30 |
| futures_bp.py | `/industry/stocks/data` | 15 |
| futures_bp.py | `/popup/overview` | 30（本次由 60 改） |
| futures_bp.py | `/zhangfu/distribution` | 30 |
| futures_bp.py | `/amountflow/data` | 60（本次由 120 改） |
| market_bp.py | `/minute/<code>` | 10 |
| market_bp.py | `/depth/<code>` | 3 |
| market_bp.py | `/tick/<code>` | 3 |
| option_bp.py | `/exquote/minute/<code>` | 10 |
| option_bp.py | `/exquote/quote/<code>` | 3 |
| option_bp.py | `/exquote/tick/<code>` | 3 |
| option_bp.py | `/full`（顶层共享缓存 `_OPT_FULL_CACHE_TTL`） | 6（另支持 `force=1`） |

---

## 六、复用要点

1. **默认 ttl 必须 ≤ 轮询周期**（宜略小几秒，避开调度抖动卡边界时偶发命中旧缓存）。
2. **改默认 ttl 要三处一起改**：模板 `force ? 0 : <默认>` / 路由 `request.args.get("ttl","<默认>")` /
   `core/ak_service.py` 函数签名。前两处重启即可；**第三处属 `core/`，必须重启服务**（模板有 `TEMPLATES_AUTO_RELOAD`）。
3. **`ttl` 是每次请求现算的参数，不是实例状态位** —— 不存在「刷新后恢复 ttl」这个动作；`ttl=0` 也不清缓存。
4. **两层缓存要分清**：服务端 ttl 缓存靠 `ttl=0` 绕过；浏览器缓存靠 URL 上 `&_=Date.now()` 绕过，两者独立。
5. **iframe 容器的「刷新」≠ 绕过缓存**：重挂 `src` 只触发子页首屏，子页走默认 ttl 会命中缓存。
   必须显式把 `force=1` 透传进子页 URL。
