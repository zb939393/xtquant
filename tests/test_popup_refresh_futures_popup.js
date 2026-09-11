/* 期指综合看盘（futures_popup.html）工具栏「刷新」接入 —— 桩环境逻辑测试
 * 用可复用骨架 require：假 document/echarts/fetch/Vue/IntersectionObserver/定时器，
 * 真跑模板内联脚本，断言：
 *   根实例 refreshAll 广播 __forcerefresh + 重取快照 + 提示；
 *   16 个格子（fut-compact-rt / fut-kline）在 force 时用 ttl=0、轮询时用默认 ttl；
 *   滚出视野时先记账（_forcePending），回到视野再用 ttl=0 取一次。
 */
const fs = require('fs');
const path = require('path');
const H = require('C:/Users/zb/.workbuddy/skills/xtquant-popup-refresh/stub_test_harness.js');
const { check, makeHarness, boot, bootComponent, buildComponent, drain, ttlOf, fireInterval, report } = H;

const FILE = 'futures_popup.html';
const ROOT = 'D:/xtquant/templates';
H.setFile(FILE);

const payload = (url) => {
  if (url.indexOf('/futures/snapshot') === 0) {
    return { ok: true, data: [{ code: 'IF2609', name: 'IF当月', price: 4000, prev_close: 3980, change_pct: 0.5 }] };
  }
  if (url.indexOf('/futures/vwap/params') === 0) return { ok: true, data: {} };
  if (url.indexOf('/futures/exquote/minute') === 0) return { ok: true, src: 'test', data: [] };
  if (url.indexOf('/futures/exquote/kline') === 0) return { ok: true, src: 'test', data: [] };
  return { ok: true, data: {} };
};

const snapOf = (st) => st.fetchUrls.filter(u => u.indexOf('/futures/snapshot') === 0);
const callsOf = (st, kw) => st.fetchUrls.filter(u => u.indexOf(kw) === 0);
const lastTtl = (st, kw) => {
  const a = callsOf(st, kw);
  return a.length ? ttlOf(a[a.length - 1]) : null;
};

(async () => {
  // ==================================================== 0. 模板静态检查
  console.log('='.repeat(70));
  console.log('0. 模板静态检查');
  console.log('='.repeat(70));
  {
    const html = fs.readFileSync(path.join(ROOT, FILE), 'utf8');
    check(/id="refreshBtn"/.test(html) || /icon="el-icon-refresh"[^>]*@click="refreshAll"/.test(html),
      '工具栏存在「刷新」按钮并绑定 refreshAll');
    check(html.indexOf('icon="el-icon-refresh"') > 0, '刷新按钮用 el-icon-refresh 图标');
    check(html.indexOf(':loading="loading"') > 0, '刷新按钮带 :loading="loading"（期间置灰）');
    const iRef = html.indexOf('@click="refreshAll"');
    const iClose = html.indexOf('@click="closeWin">关闭');
    check(iRef > 0 && iClose > iRef, '按钮顺序：刷新 在 关闭 之前');
    check((html.match(/window\.addEventListener\('__forcerefresh'/g) || []).length === 2,
      '两个组件都注册了 __forcerefresh 监听 (2)');
    check((html.match(/window\.removeEventListener\('__forcerefresh'/g) || []).length === 2,
      '两个组件都在销毁时摘除监听 (2)');
    check((html.match(/refreshAll\(\)\{/g) || []).length === 1, 'refreshAll 定义唯一');
    check((html.match(/_flashMsg\(t\)\{/g) || []).length === 1, '_flashMsg 定义唯一');
    check((html.match(/_onForce\(\)\{/g) || []).length === 2, '_onForce 每组件各定义一次 (2)');
    check(/ttl=' \+ \(force \? 0 : 3\)/.test(html), '分时：force→ttl=0，轮询 ttl=3');
    check(/ttl=' \+ \(force \? 0 : 15\)/.test(html), 'K线：force→ttl=0，轮询 ttl=15');
  }

  // ==================================================== A. 根实例
  console.log('');
  console.log('='.repeat(70));
  console.log('A. 根实例 refreshAll');
  console.log('='.repeat(70));
  {
    const st = await boot(FILE, { payload });
    check(st.components['fut-compact-rt'] && st.components['fut-kline'], '两个子组件均已注册');
    check(st.app.rows.length === 1 && st.app.rows[0].code === 'IF2609', '首屏快照写入 rows');
    check(snapOf(st).length === 1, '首屏取了一次快照 (实际 ' + snapOf(st).length + ')');
    check(st.app.loading === false, '首屏后 loading 复位');
    check(/^\d/.test(st.app.updatedAt || ''), 'updatedAt 已更新 (' + st.app.updatedAt + ')');
    check(/^\d/.test(st.app.statusMsg || '') === false, '首屏无提示文案');

    // —— 点「刷新」
    const nSnap = snapOf(st).length;
    st.app.refreshAll();
    check(st.dispatched.indexOf('__forcerefresh') >= 0, '刷新派发了 __forcerefresh 广播');
    check(st.app.loading === true, '刷新中 loading=true（按钮置灰）');
    check(snapOf(st).length === nSnap + 1, '刷新重取了快照 (+1)');
    await drain(st, 100);   // 只推进 100ms：让请求链跑完，但不误触发 2.5s 的提示清除
    check(st.app.loading === false, '刷新完成后 loading 复位');
    check(/^已刷新 \d/.test(st.app.statusMsg || ''), '刷新成功给出提示 (' + st.app.statusMsg + ')');

    // —— loading 中重复点击被忽略
    st.app.loading = true;
    const nSnap2 = snapOf(st).length, nDisp = st.dispatched.length;
    st.app.refreshAll();
    check(snapOf(st).length === nSnap2 && st.dispatched.length === nDisp, 'loading 中重复点击被忽略');
    st.app.loading = false;

    // —— 提示自动消失（2500ms）
    await drain(st, 3000);
    check(st.app.statusMsg === '', '提示 2.5s 后自动清空');
  }
  {
    // 失败路径：不能被「已刷新」覆盖
    const st = await boot(FILE, { payload });
    st.fetchMode = 'fail';
    st.app.refreshAll();
    await drain(st);
    check(/^期指数据加载失败/.test(st.app.statusMsg || ''), '失败时保留错误提示 (' + st.app.statusMsg + ')');
    check(st.app.loading === false, '失败后 loading 复位（按钮不卡死）');
  }

  // ==================================================== B. fut-compact-rt（分时）
  console.log('');
  console.log('='.repeat(70));
  console.log('B. fut-compact-rt（分时格子）');
  console.log('='.repeat(70));
  {
    const st = makeHarness(FILE, { payload, io: true });
    const c = await bootComponent('fut-compact-rt', st, { code: 'IF2609', prevClose: 3980, price: 4000, chgPct: 0.5 });
    check(st.io && st.io.observed() === 1, '假 IntersectionObserver 已就绪且 observe 自身');
    check(callsOf(st, '/futures/exquote/minute').length === 0, '未进入视野时不取数（省流）');

    // 滚出视野时点刷新 → 只记账
    c._onForce();
    check(c._forcePending === true, '不可见时 _onForce 只记 _forcePending');
    check(callsOf(st, '/futures/exquote/minute').length === 0, '不可见时不发请求');

    // 回到视野 → start() 消费 pending，用 ttl=0 强制取一次
    st.io.trigger(true);
    await drain(st);
    check(callsOf(st, '/futures/exquote/minute').length === 1, '可见后立即取一次 (+1)');
    check(lastTtl(st, '/futures/exquote/minute') === 0, '可见后消费 pending → ttl=0 (实际 ' + lastTtl(st, '/futures/exquote/minute') + ')');
    check(!c._forcePending, '_forcePending 已消费清零');

    // 轮询维持 ttl=3
    const ivIdx = st.intervals.length - 1;
    const n = callsOf(st, '/futures/exquote/minute').length;
    check(fireInterval(st, ivIdx) === true, '3s 轮询定时器在视野内可触发');
    await drain(st);
    check(callsOf(st, '/futures/exquote/minute').length === n + 1, '3s 轮询取数 (+1)');
    check(lastTtl(st, '/futures/exquote/minute') === 3, '轮询用 ttl=3 (实际 ' + lastTtl(st, '/futures/exquote/minute') + ')');

    // 可见时点刷新 → 立刻 ttl=0
    c._onForce(); await drain(st);
    check(lastTtl(st, '/futures/exquote/minute') === 0, '可见时 _onForce 立刻 ttl=0');
    check(!c._forcePending, '可见时不留 pending');

    // 滚出视野 → 轮询被 clearInterval（桩会拒绝触发已清除的定时器，等价浏览器语义）
    st.io.trigger(false);
    const n2 = callsOf(st, '/futures/exquote/minute').length;
    check(fireInterval(st, ivIdx) === false, '滚出视野后该定时器已被 clearInterval');
    await drain(st);
    check(callsOf(st, '/futures/exquote/minute').length === n2, '滚出视野后不再取数（轮询停止）');

    // 销毁后不再响应广播
    const opt = st.components['fut-compact-rt'];
    opt.beforeDestroy.call(c);
    check(st.listeners['__forcerefresh'] === undefined || st.listeners['__forcerefresh'].length === 0,
      '销毁后 __forcerefresh 监听已摘除');
  }

  // ==================================================== C. fut-kline（K线）
  console.log('');
  console.log('='.repeat(70));
  console.log('C. fut-kline（K线格子）');
  console.log('='.repeat(70));
  {
    const st = makeHarness(FILE, { payload, io: true });
    const c = await bootComponent('fut-kline', st, { code: 'IF2609', period: '5min', count: 120, indicator: 'mfi' });
    check(callsOf(st, '/futures/exquote/kline').length === 0, '未进入视野时不取数');

    c._onForce();
    check(c._forcePending === true, '不可见时只记账');
    st.io.trigger(true); await drain(st);
    check(lastTtl(st, '/futures/exquote/kline') === 0, '回到视野 → ttl=0 (实际 ' + lastTtl(st, '/futures/exquote/kline') + ')');

    const ivIdx = st.intervals.length - 1;
    const n = callsOf(st, '/futures/exquote/kline').length;
    check(fireInterval(st, ivIdx) === true, '3s 轮询定时器在视野内可触发');
    await drain(st);
    check(callsOf(st, '/futures/exquote/kline').length === n + 1, '3s 轮询取数 (+1)');
    check(lastTtl(st, '/futures/exquote/kline') === 15, '轮询用 ttl=15 (实际 ' + lastTtl(st, '/futures/exquote/kline') + ')');

    c._onForce(); await drain(st);
    check(lastTtl(st, '/futures/exquote/kline') === 0, '可见时 _onForce 立刻 ttl=0');

    // _redraw 走的是非 force 路径（不应穿透缓存）
    c._onZoom(); await drain(st);
    check(lastTtl(st, '/futures/exquote/kline') === 15, '_redraw 仍用 ttl=15（不误伤缓存）');
  }

  // ==================================================== D. 端到端：广播联动
  console.log('');
  console.log('='.repeat(70));
  console.log('D. 端到端：根实例点刷新 → 两个格子都被强制重取');
  console.log('='.repeat(70));
  {
    const st = makeHarness(FILE, { payload, io: true });
    const root = st.app;
    const rt = buildComponent('fut-compact-rt', st, { code: 'IF2609', prevClose: 3980, price: 4000, chgPct: 0.5 });
    const kl = buildComponent('fut-kline', st, { code: 'IF2609', period: '1min', count: 120, indicator: 'mfi' });
    st.components['fut-compact-rt'].mounted.call(rt);
    st.components['fut-kline'].mounted.call(kl);
    await drain(st);
    // 两个格子都进入视野
    st.io.trigger(true);
    await drain(st);
    rt._forcePending = false; kl._forcePending = false;
    const nRt = callsOf(st, '/futures/exquote/minute').length;
    const nKl = callsOf(st, '/futures/exquote/kline').length;

    root.refreshAll();
    await drain(st);
    check(callsOf(st, '/futures/exquote/minute').length === nRt + 1, '分时格子被强制重取 (+1)');
    check(callsOf(st, '/futures/exquote/kline').length === nKl + 1, 'K线格子被强制重取 (+1)');
    check(lastTtl(st, '/futures/exquote/minute') === 0 && lastTtl(st, '/futures/exquote/kline') === 0,
      '两者都以 ttl=0 穿透服务端缓存');
    check(st.app.rows.length === 1, '快照也一并刷新');
  }

  report();
})();
