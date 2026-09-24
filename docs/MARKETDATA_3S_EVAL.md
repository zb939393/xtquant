# 行情接收「3 秒更新」可行性评估

> 评估对象：`D:\xtquant` 期指行情接收链路（futures_popup → `/futures/exquote/kline` + `/futures/snapshot` → `fut_bars` → 扩展行情主站池 7721/7727/7730）。
> 评估时间：2026-09-12（周六，非交易时段，故实时 tick 不流动，但历史 K线 / 实时快照接口均可取数）。
> 方法：直接测量取数路径延迟（connect + `get_instrument_bars` / `get_instrument_quote`），并核对轮询间隔与缓存 TTL 配置。

## 结论（先说答案）

**机制速度完全够 3 秒——实测取数 0.08~0.6s，远低于 3s 预算。但当前配置达不到 3 秒刷新，被两道「配置闸」卡住：**

1. K线服务端缓存 `ttl=15s`（`api/futures_bp.py:188`）→ 同一份数据 15s 内重复请求都返回旧值；
2. 实时快照轮询间隔 `refreshInterval=5000ms`（`templates/futures_popup.html:900`）→ 实时价每 5s 才刷一次。

**改两处配置即可让「实时价」稳定 3s 刷新；若要求 K线当前形成中的 bar 也 3s 新鲜，需再把 K线 ttl 降到 3（见第五节）。**

## 一、实测延迟预算（关键证据）

直接打扩展行情主站（绕开 Flask，因沙箱代理拦截 `127.0.0.1`，但 Flask JSON 序列化开销 <~80ms，不影响结论）：

| 环节 | 实测耗时 | 样本 |
|---|---|---|
| `_get_api()` 选路+建连（router.select → TdxExHq_API.connect） | 19~102ms，均值 66ms | n=12 |
| `get_instrument_bars` IFL9 1min（count=700） | 69~276ms | ×3 |
| `get_instrument_bars` IFL9 5min | 61~594ms | ×3 |
| `get_instrument_bars` IFL9 30min | 48~75ms | ×3 |
| `/futures/snapshot`（4 合约 get_instrument_quote） | 79~212ms | ×4 |

**单合约单次完整取数（建连+取数）最差约 0.6s，典型 0.1~0.3s。** 浏览器侧总耗时 ≈ TDX 取数 + Flask 开销 ≈ 0.1~0.7s，距 3s 有 4~30 倍余量。

最坏情况天花板：`_EXHQ_TIMEOUT=6.0s`（连接+收包各 6s），失败换节点重试一次 → 极端最坏 ~12s，但选路器挑快节点 + 连续失败降权冷却，常态不会触发。

## 二、当前配置为什么「达不到 3 秒」

| 数据 | 客户端轮询 | 服务端缓存 | 实际刷新节奏 | 与 3s 的差距 |
|---|---|---|---|---|
| K线 1m/5m/30m | `setInterval(3000)`（`futures_popup.html:798`） | `ttl=15`（`futures_bp.py:188`） | **15s**（轮询虽 3s，但 15s 内命中缓存返回旧值） | 5× 超标 |
| 实时价/涨跌/持仓（snapshot） | `refreshInterval=5000`（`futures_popup.html:900`） | 无（每次现算） | **5s** | 1.7× 超标 |

要点：K线轮询已是 3s，但 `ttl=15` 让前 14s 的请求都吃到缓存旧数据，等于「假 3s」。这是最容易被误判的地方。

## 三、并发与限流余量（决定「全格子每 3s 打一次」是否压垮券商）

- 综合看盘共 16 格（4 合约 × {1m,5m,30m,分时}），其中 12 个 K线格各自 period 不同 → 最多 12 个不同缓存键。
- 每 3s 强制刷新 = 4 req/s 持续。单请求 ~0.3s，`_EXHQ_SEM` 并发上限 20，单 flight 合并窗口 0.25s（`_SF_COALESCE`）。
- **4 req/s ≪ 20 并发上限**，且新建连接每次 ~0.07s，券商 32 台池可承受；但持续 4/s 仍属偏高，建议用 `ttl=3` 折中（见下）而非 `ttl=0` 全量穿透。

## 四、达到 3 秒的两档方案

**档 A（推荐，实时价 3s，K线结构 15s）— 改动最小、零风险**
- `futures_popup.html:900` `refreshInterval: 5000` → `3000`。
- 实时价/涨跌/持仓立即变 3s 刷新；K线历史结构保持 15s 缓存（最后一根形成中的 bar 在 15s 内也会被重新取回，足够日内策略看结构）。
- 实测 snapshot 4 合约 0.08~0.21s，3s 窗口内轻松完成。

**档 B（严格 3s，含 K线当前 bar）— 需关注券商负载**
- 上述 `refreshInterval→3000` 不变；
- K线请求 `ttl` 由 15 改为 **3**（`futures_popup.html:828` 的 `ttl=` 与 `futures_bp.py:188` 默认值同步改 3），轮询保持 3000。
- 效果：每 3s 窗口内 K线重新打 TDX，当前形成 bar 的新鲜度 ≤3s；singleflight 把瞬时并发压到 ≤12 键，安全。
- 风险：持续 ~4 req/s 打扩展行情主站，长交易时段需观察券商是否限频；可用 `_EXHQ_SEM` / `_SF_COALESCE` 再兜底。

> 不建议 `ttl=0`（每次强制穿透）：等效档 B 但彻底放弃缓存，券商压力最大，无收益。

## 五、附带结论

- **延迟不是瓶颈**：扩展行情主站就近节点建连 ~20~100ms、取数 ~50~300ms，机制本身支持亚秒级更新。
- **真正的瓶颈是缓存 TTL 与轮询间隔两处常量**，均为一行配置，无架构改动。
- 与上一轮 `pytdx_patches` 评估衔接：解析层修复的是「数据正确性」（指数 DWM 乱码等），不影响更新频率；3s 达标与否与补丁无关。

## 六、验证建议

交易时段内（盘中 9:30~15:00）用档 A/B 启动后，浏览器开 F12 看 `/futures/snapshot` 与 `/futures/exquote/kline` 的响应 `Date` 间隔，确认 ≤3s；并观察 `_fetch_index_*` / 主站日志有无限频报错。
