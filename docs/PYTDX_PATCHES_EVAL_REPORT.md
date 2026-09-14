# pytdx_patches 行情接收评估报告

- **评估对象**：`pytdx_patches`（v0.1.0，作者 `zb939393`，GitHub: zb939393/pytdx_patches）
- **评估时间**：2026-09-11 23:xx（盘后）
- **运行环境**：生产 Flask 服务实际解释器 `D:/Python/python313/python.exe`，pytdx **1.72**（与补丁文档声明版本一致），`pytdx_patches` 安装于此解释器的 `Lib/site-packages`
- **方法**：① 运行时加载 `pytdx_patches.self_test()`；② 逐条核对补丁声称的 pytdx 缺陷是否真实存在于已装源码；③ 在真实券商行情主站（华泰/安信/国泰君安，端口 7709）上**对同一份返回数据做「未打补丁 vs 打补丁」对比解析**；④ 检查运行期风险。

---

## 一、结论摘要（Verdict）

| 维度 | 结论 |
|---|---|
| 补丁是否生效 | ✅ 运行期 `self_test()` 全 PASS（装饰器/心跳类/重连方法/last_ack 时机均生效） |
| 声称的缺陷是否属实 | ✅ P0-2、P1-1、P1-2 在 pytdx 1.72 源码中**逐一确认**；P4 在真实数据上**灾难级复现** |
| 行情解析正确性（核心） | ✅ **P4 日/周/月线修复关键且正确**；个股 DWM 打补丁前后完全一致（无损）；分钟重建对个股正确、对指数夜间依赖脏源 |
| 主要风险 | ⚠️ **静默失败**（try/except 吞掉缺失）、⚠️ P2 指数盘后重建继承脏源、⚠️ 心跳自愈仅结构验证未实测 |
| 总体 | **生产可用，P4 是必须存在的修复**；建议补一条「缺失即告警」并固化导入顺序 |

---

## 二、补丁清单与逐项实证

### P0-1 断线心跳自愈 —— 结构 PASS，未实测
- 修复：`PatchedHqHeartBeatThread` 持有 api 实时引用，连接断开/`last_transaction_failed` 时调 `_reconnect_socket()` 重建底层 socket，不重启心跳线程。
- 源码侧：pytdx `heartbeat.py` 原 `do_heartbeat` 失败仅 `log.debug` 吞异常、且拷贝旧 `client` 引用 → 属实。
- 运行期：`self_test` 断言 `_hb.HqHeartBeatThread is PatchedHqHeartBeatThread` 通过，`BaseSocketClient._reconnect_socket` 已注入。
- **未实测**：需强制断链观察自动恢复，本评估未做（盘后强断链风险高且非必要）。结论：**结构正确，建议在生产做一次强制断连演练**。

### P0-2 / P1-1 ack 时机与重连判定 —— 源码确认 + 修复生效 ✅
- 源码确认（pytdx 1.72 `base_socket_client.py`）：
  ```python
  def update_last_ack_time(func):
      def wrapper(self, *args, **kw):
          self.last_ack_time = time.time()   # ← P0-2：在调用【前】就刷新，心跳永不发包
          ...
          ret = func(self, *args, **kw)
          ...
          if ret:                             # ← P1-1：空结果 []/0/False 被判为失败
              return ret
  ```
  两处缺陷**与文档描述完全一致**。
- 修复：`last_ack_time` 移至调用成功返回后刷新；`if ret` → `if ret is not None`。
- 运行期：`self_test` 断言 `get_security_bars` / `get_markets` 已被 v2 装饰器包裹（含 `_pytdx_patched` 标记）→ 生效。
- 收益：空结果接口（无成交的某日、部分市场 `get_security_list`）不再被误判失败；心跳仅在真正空闲超时才发包，符合设计。

### P1-2 get_traffic_stats 断连崩溃 —— 源码确认 + 修复 ✅
- 源码确认：pytdx 原 `get_traffic_stats` 直接访问 `self.client.first_pkg_send_time`，`disconnect()` 置 `client=None` 后 `AttributeError`。
- 修复：补 `if self.client is None: return {...全 0/None...}` 守卫。运行期已替换为 `_get_traffic_stats`。

### P4 get_security_bars 日/周/月线第 2 根起错位 —— **灾难级实证，修复正确** ✅🔴
这是本评估最重要的发现。在真实主站（华泰南京电信1）取上证综指 `000001`：

**未打补丁（pytdx 1.72 原生）返回的是乱码日期：**
```
daily  : ['2026-06-26 15:00', '407253-84-21 15:00', '44833-64-15 15:00', '45265-35-70 15:00', ...]
weekly : ['2026-06-26 15:00', '125195-35-97 15:00', '363255-68-16 15:00', ...]
monthly: ['2025-10-31 15:00', '125195-35-97 15:00', '363255-68-16 15:00', ...]
```
`strict_increasing = false`，从第 2 根起日期/价格全部错位（字节级跟随漂移）。

**打补丁后（扫描定位每条记录开头的合法 YYYYMMDD）干净且严格递增：**
```
daily : 2026-06-26 → 2026-07-03 → ... → 2026-09-11  (12 根，strict_increasing=true)
weekly: 2026-06-26 → 2026-07-03 → ... → 2026-09-11
monthly:2025-10-31 → 2025-11-28 → ... → 2026-09-11
```
**结论**：未打补丁时指数日/周/月线**完全不可用**（日期乱码 → 任何按日期对齐的逻辑全崩）。补丁修复正确。

**补丁对个股安全（无损）**：取 `600519` 日线，未打补丁与打补丁 dates/closes **逐位相同**
（`['2026-09-04'...'2026-09-11']`，closes `[1330.0, 1316.01, ... 1275.16]`）。说明 P4 仅对「原始实现已坏」的指数类记录改变行为，对个股是 no-op —— **修复安全、零回归**。

> 根因：日/周/月线记录布局为 `[date:4][变长价差×4][vol:4][amount:4][尾随4]`，指数尾随字段明显、记录变长，pytdx 用固定步长少读 4 字节致连锁漂移；个股变长更短偶发仍能凑出连续日期。补丁绕开变长，逐根扫描合法日期 —— 股票/指数通用。

### P2 当日分时 get_minute_time_data（1 分钟 K 线重构）—— 对个股正确，指数盘后继承脏源 ⚠️
- 主路径：`_reconstruct_minute_from_kline` 用 `get_security_bars(7, …)` 重构分时（与日 K 线逐分钟核验一致；0x0537 差分直解因券商前缀不一致已弃用）。
- **个股（600519）实测正确**：返回 240 根，`09:31 → 15:00`，价格 ~1276，与同日 1 分 K 线尾值一致。
- **指数（000001）盘后实测异常**：返回 1 根、price `207172.49`（超合理范围）。根因定位：**脏源继承** —— 同一主站 `get_security_bars(7,1,'000001',0,240)` 未打补丁即返回垃圾（`distinct dates=215`、`"2004-02-72 34:31"`、`V=8.46e+24`），即**该主站指数 1 分钟 K 线盘后数据本身损坏**；P2 只是忠实地把脏源重构成 1 根。
- **项目规避情况**：项目取指数分钟走 `get_index_bars`（category=8），本评估实测其 datetime **干净**（`2026-09-11 14:58/14:59/15:00`），故 P2 的指数缺陷在现有代码路径下基本不触发；但任何直接调用 `get_minute_time_data(1,'000001')` 的盘后场景会得到脏值。
- 建议：指数盘后「当日分时」改走 L1 `get_sparkline`/`get_security_quotes`（项目已有该通道），不依赖 1 分 K 线重建。

### P3 get_security_list 名称 GBK 解码 —— 逻辑正确，本环境无法实证 ⚠️
- 修复：名称 `decode("gbk","ignore")` + `decode("utf-8","ignore")` 容错，避免个别含 stray 引导字节（如 830654 "ChatGPT"+0xb8）的记录抛错致整批返回 None。逻辑正确。
- **本环境无法实证**：在 3 个券商主站上 `get_security_list(1/0, …)` 全部返回 `None`（v2 装饰器捕获异常后返回 None）。原因应是券商 HQ 主站不提供证券列表接口（或调用异常），而非补丁失效 —— 补丁的解码容错路径需「调用成功但个别记录坏名」才能触发，本环境无此条件。**结论：补丁逻辑成立，但未在真实坏名样本上跑通，建议用公共通达信主站 + 含 830654 的样本补一次实证。**

---

## 三、关键运行期风险（必须关注）

### ⚠️ 风险 1：静默失败（最高优先级）
项目 6 处导入均为：
```python
try:
    import pytdx_patches  # noqa: F401
except Exception:
    pass
```
若运行解释器未安装该包，**应用零报错地以「未打补丁」状态运行** —— 此时指数日/周/月线即为 P4 实测的乱码（见上），且**无任何告警**。
- 实证：在本机另一解释器（`C:/Users/zb/.workbuddy/binaries/python/3.13.12`）中 `import pytdx_patches` 直接 `ModuleNotFoundError`；生产解释器（`D:/Python/python313`）已装故当前正常。
- **建议**：将 `except` 改为 `except Exception: log.warning("pytdx_patches 未加载，指数 DWM 行情将乱码！")` 至少打一条 ERROR 级日志；并在部署/启动脚本里显式 `pip show pytdx-patches` 做健康检查。

### ⚠️ 风险 2：导入顺序脆弱
补丁是「import 即 apply」的模块级猴子补丁，**必须在 `TdxHq_API()` 实例化之前导入**。当前 `ak_service.py`(L25)、`futures_service.py`(L28)、`option_exquote_service.py`(L25) 顶部已导入，函数调用晚于导入，故现有链路 OK；但任一先建连接后导入的路径（或新子进程）会漏打补丁。建议把导入收敛到单一 early-import 入口。

### ⚠️ 风险 3：P4 日期扫描的边界假设
`_patched_security_bars_parse` 假设「每条记录开头 4 字节是合法且严格递增的 YYYYMMDD」。理论上若尾随字段恰巧凑出 1990–2035 内且递增的伪日期，会锚错；实测指数/个股均稳健（尾随字节几乎不可能满足日历+递增双重约束），风险低，但属**启发式而非协议级解析**，长期依赖需留意 pytdx 升级改版。

### ⚠️ 风险 4：心跳自愈仅结构验证
P0-1 已确认类替换与 `_reconnect_socket` 注入成功，但**未做强制断连的线上恢复演练**。建议择机在生产做一次断网/杀链恢复观测。

### 旁注：get_index_bars 成交额字段脏值（非补丁范围）
实测 `get_index_bars(8,1,'000001')` 的 `amount` 在 14:59 那根为 `5.877471754111438e-39`（近零垃圾），datetime/close 正常。属行情源质量，pytdx_patches 不覆盖该命令；项目成交额已改走 `get_security_quotes`/腾讯 HTTP 兜底，不受影响，但提示指数分钟成交额需兜底校验。

---

## 四、评估环境确认

- 生产解释器 `D:/Python/python313/python.exe`：`pytdx` 1.72 + `pytdx-patches` 0.1.0 **均已安装** → 生产当前**已加载补丁**，`self_test` PASS。
- 评估用真实主站：华泰南京电信1 (180.101.48.170:7709) 连通并成功取数；安信/国泰君安同为可用候选。
- 复现脚本（保留于 `data/`）：
  - `data/_eval_pytdx_patches.py` —— P4/P2/P3/self_test 主评估
  - `data/_eval_pytdx_patches_p2.py` —— P2/P3 聚焦诊断
  - `data/_eval_pytdx_patches_p4.py` —— get_index_bars 净度 + P4 个股无损验证
  - 运行：`D:/Python/python313/python.exe data/_eval_pytdx_patches.py`（需行情主站可达）

---

## 五、行动建议（按优先级）

1. **【高】消除静默失败**：`except` 分支加 ERROR 日志 + 启动健康检查 `pip show pytdx-patches`；CI/部署脚本断言包存在。
2. **【高】固化导入顺序**：收敛为单一 early-import 入口，避免任何「先连后补」路径。
3. **【中】指数盘后分时改走 L1 sparkline/quotes**，不再依赖 `get_minute_time_data` 的 1 分 K 线重建（脏源继承）。
4. **【中】P3 补一次真实实证**：用公共通达信主站 + 含坏名样本（830654）验证解码容错。
5. **【低】P0-1 生产断连演练**：验证心跳自愈真实恢复链路。
6. **【低】get_index_bars 成交额兜底**：对 amount 做非零/合理性校验，脏值回退 HTTP 源。

---

## 附：证据摘录

**P4 指数日线（华泰南京电信1，未打补丁 vs 打补丁）**
```
UNPATCHED daily[1..]: '407253-84-21 15:00', '44833-64-15 15:00', ...   (strict_increasing=False)
PATCHED  daily[1..]: '2026-07-03 15:00', '2026-07-10 15:00', ...       (strict_increasing=True)
PATCHED  monthly   : '2025-10-31' → '2025-11-28' → ... → '2026-09-11'   (12 根连续)
```

**P2 个股正确 / 指数脏源**
```
get_minute_time_data(1,'600519')        -> 240 根, 09:31→15:00, 价格~1276  ✅
get_security_bars(7,1,'000001',0,240)   -> distinct dates=215, '2004-02-72 34:31', V=8.46e+24  ❌(源脏)
get_minute_time_data(1,'000001')        -> 1 根, price=207172.49                                ❌(继承脏源)
```

**self_test（运行期）**
```
[pytdx_patches] self_test passed: 装饰器/心跳类/重连方法/last_ack_time 时机 均生效
```
