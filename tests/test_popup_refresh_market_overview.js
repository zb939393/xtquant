/* A 股市场概况（market_overview.html，路由 /overview 与首页 /）—— ttl 与轮询对齐修复的回归测试
 *
 * 背景（缺陷 1）：原来 URL 写死 `?ttl=60`，而轮询是 30s ——
 *   ① 缓存(60s) > 轮询间隔(30s) → 每两次轮询只有一次真取数，数据实际每 60s 才更新（脚注却写"每 30 秒自动刷新"）；
 *   ② 刷新按钮 `fetchData(true)` 的 force 被忽略（URL 写死 60）→ 点刷新打的是缓存。
 * 修复后：`const ttl = force ? 0 : 30`，默认值与 30s 轮询对齐，force 真正映射为 ttl=0。
 *
 * 断言要点：
 *   首屏 fetchData(true) → ttl=0；轮询 fetchData(false) → ttl=30；
 *   刷新 → ttl=0；loading 期间重复点击被忽略；失败路径 loading 复位且提示不丢。
 */
const fs = require('fs');
const path = require('path');
const H = require('C:/Users/zb/.workbuddy/skills/xtquant-popup-refresh/stub_test_harness.js');
const { check, boot, drain, ttlOf, fireInterval, report } = H;

const FILE = 'market_overview.html';
const ROOT = 'D:/xtquant/templates';
H.setFile(FILE);

const POLL_MS = 30000;        // 模板里的 setInterval 间隔
const EXPECT_TTL = 30;        // 与上面的间隔对齐的默认 ttl

const payload = () => ({
  ok: true,
  data: { ok: true, indices: [], up: 931, down: 4192, flat: 83, limit_up: 20, limit_down: 3, total_amount: 1e12, pool: 5206 },
});

const callsOf = (st) => st.fetchUrls.filter(u => u.indexOf('/futures/popup/overview') === 0);
const lastTtl = (st) => { const a = callsOf(st); return a.length ? ttlOf(a[a.length - 1]) : null; };

(async () => {
  // ==================================================== 0. 模板静态检查
  console.log('='.repeat(70));
  console.log('0. 模板静态检查');
  console.log('='.repeat(70));
  {
    const html = fs.readFileSync(path.join(ROOT, FILE), 'utf8');
    check(html.indexOf('overview?ttl=60') < 0, '已无写死的 ttl=60');
    check(/'\/futures\/popup\/overview\?ttl=' \+ ttl/.test(html), 'URL 用 ttl 变量拼接');
    check(/const ttl = force \? 0 : 30/.test(html), 'ttl 计算式 = force ? 0 : 30');
    check((html.match(/const ttl = force \? 0 : \d+/g) || []).length === 1, 'ttl 计算式只出现一次');
    check(html.indexOf('@click="fetchData(true)"') > 0, '刷新按钮绑定 fetchData(true)');
    check(html.indexOf(':disabled="loading"') > 0, '刷新按钮带 :disabled="loading"（期间置灰）');
    check(html.indexOf('this.fetchData(false), 30000') > 0, '轮询间隔 30000ms');
    check(html.indexOf('每 30 秒自动刷新') > 0, '脚注文案与轮询间隔一致');
    check((html.match(/fetchData\(force\)\{/g) || []).length === 1, 'fetchData 定义唯一');
    check((html.match(/reqJSON\(/g) || []).length >= 2, 'reqJSON 已定义且被调用');
    // 核心对齐断言：默认 ttl(秒) ≤ 轮询间隔(秒)
    check(EXPECT_TTL * 1000 <= POLL_MS, '默认 ttl(' + EXPECT_TTL + 's) ≤ 轮询间隔(' + (POLL_MS / 1000) + 's)');

    // 反例留档：修复前的写法会直接违反上面这条判据
    const before = "const url = '/futures/popup/overview?ttl=60&_=' + Date.now();";
    check(/ttl=60/.test(before) && 60 * 1000 > POLL_MS, '（反例）修复前 ttl=60 确实 > 30s 间隔 → 判定为错配');
  }

  // ==================================================== A. 首屏 / 轮询 / 刷新 的 ttl
  console.log('');
  console.log('='.repeat(70));
  console.log('A. 首屏 / 轮询 / 刷新的 ttl 语义');
  console.log('='.repeat(70));
  {
    const st = await boot(FILE, { payload });
    check(callsOf(st).length === 1, '首屏只取一次 (实际 ' + callsOf(st).length + ')');
    check(lastTtl(st) === 0, '首屏 created() 走 fetchData(true) → ttl=0 (实际 ' + lastTtl(st) + ')');
    check(st.app.overview.up === 931 && st.app.overview.down === 4192,
      '首屏数据写入 overview (涨' + st.app.overview.up + ' / 跌' + st.app.overview.down + ')');
    check(st.app.loading === false, '首屏后 loading 复位');
    check(/^\d/.test(st.app.updatedAt || ''), 'updatedAt 已更新时间戳 (' + st.app.updatedAt + ')');

    check(st.intervals.length === 1, '注册了 1 个轮询定时器');
    check(st.intervals[0].delay === POLL_MS, '轮询间隔 = ' + (POLL_MS / 1000) + 's (实际 ' + st.intervals[0].delay + 'ms)');

    // —— 轮询：应带默认 ttl
    const n0 = callsOf(st).length;
    fireInterval(st);
    await drain(st);
    check(callsOf(st).length === n0 + 1, '轮询触发一次取数 (+1)');
    check(lastTtl(st) === EXPECT_TTL, '轮询用 ttl=' + EXPECT_TTL + ' (实际 ' + lastTtl(st) + ')');

    // —— 点「刷新」：应带 ttl=0（本次修复的核心）
    st.app.fetchData(true);
    await drain(st);
    check(lastTtl(st) === 0, '刷新 → ttl=0，真正绕过缓存 (实际 ' + lastTtl(st) + ')');

    // —— loading 中重复点击被忽略
    st.app.loading = true;
    const n1 = callsOf(st).length;
    st.app.fetchData(true);
    check(callsOf(st).length === n1, 'loading 中重复点击被忽略');
    st.app.loading = false;

    // —— 再轮询一次，确认没有"卡在 ttl=0"（每次现算、无需复位）
    fireInterval(st);
    await drain(st);
    check(lastTtl(st) === EXPECT_TTL, '刷新之后再轮询，ttl 自动回到 ' + EXPECT_TTL + '（现算，无需复位）');
  }

  // ==================================================== B. 失败路径
  console.log('');
  console.log('='.repeat(70));
  console.log('B. 失败路径');
  console.log('='.repeat(70));
  {
    const st = await boot(FILE, { payload });
    st.fetchMode = 'fail';
    st.app.fetchData(true);
    await drain(st);
    check(st.app.loading === false, '失败后 loading 复位（按钮不卡死）');
    check(/^加载失败/.test(st.app.updatedAt || ''), '失败给出提示 (' + st.app.updatedAt + ')');
  }
  {
    const st = await boot(FILE, { payload });
    st.fetchMode = 'http500';
    st.app.fetchData(true);
    await drain(st);
    check(st.app.loading === false, 'HTTP 500 后 loading 复位');
    check(/^加载失败/.test(st.app.updatedAt || ''), 'HTTP 500 给出提示 (' + st.app.updatedAt + ')');
  }

  report();
})();
