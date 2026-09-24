/* 期权自选窗口「刷新」不透传格子 的回归测试（缺陷 3）
 *
 * 背景：`option_watchlist_popup.html`（路由 /option/popup，也是综合窗口的第 8 个 iframe）
 *   工具栏「刷新」原来只做 `reload(true)` → `/option/full?force=1`，**列表确实刷新了**；
 *   但每张卡片的 `opt-board-rt` 格子取的三个接口 ttl 写死：
 *       分时 /option/exquote/minute  ttl=10
 *       盘口 /option/exquote/quote   ttl=3
 *       逐笔 /option/exquote/tick    ttl=3
 *   → 不吃 force 参数 → **列表换了、格子里的分时和盘口还是旧的**。
 *
 * 修法（照 futures_popup 的成熟做法）：
 *   - 组件监听 `window.__forcerefresh`，收到后这条链 force=true → ttl=0；
 *   - 组件被 IntersectionObserver 节流：没进视野不立刻取数，先记 `_forcePending`，
 *     回到视野首绘时消费它（否则「刷新」对滚动外的卡片完全无效）；
 *   - 根实例工具栏改 `refreshAll()` = 广播 + `reload(true)`；
 *   - 支持 `?force=1`（综合窗口「刷新全部」重载本页）在父 `mounted` 广播一次。
 *
 * 断言要点：
 *   A. 模板静态检查（监听/摘除/透传链/按钮绑定/无写死 ttl）；
 *   B. 可见的格子收到广播 → 三个接口立刻 ttl=0；之后轮询回到 10/3/3（force 不污染轮询）；
 *   C. 未进视野的格子收到广播 → 记账；广播时**一个请求都不发**；回到视野首绘即 ttl=0；
 *   D. 根实例：refreshAll 广播 + 列表带 force=1；轮询不带 force；loading 中点击被忽略；
 *   E. ?force=1 时父 mounted 广播（综合窗口入口），不带则不广播；
 *   F. beforeDestroy 摘除监听（销毁后广播不再引发请求）。
 */
const fs = require('fs');
const path = require('path');
const H = require('C:/Users/zb/.workbuddy/skills/xtquant-popup-refresh/stub_test_harness.js');
const { check, makeHarness, buildComponent, boot, drain, ttlOf, fireInterval, report } = H;

const ROOT = 'D:/xtquant/templates';
const FILE = 'option_watchlist_popup.html';
const read = (f) => fs.readFileSync(path.join(ROOT, f), 'utf8');

const CODE = '10010973';
const PROPS = { code: CODE, prevClose: 0.05, price: 0.06, chgPct: 20, delta: 0.3, kind: 'option' };

const MIN = '/option/exquote/minute';
const QTE = '/option/exquote/quote';
const TCK = '/option/exquote/tick';

const callsOf = (st, prefix) => st.fetchUrls.filter(u => u.indexOf(prefix) === 0);
const lastTtlOf = (st, prefix) => { const a = callsOf(st, prefix); return a.length ? ttlOf(a[a.length - 1]) : null; };
const ttls3 = (st) => [lastTtlOf(st, MIN), lastTtlOf(st, QTE), lastTtlOf(st, TCK)].join('/');

// 列表接口：watchlist 返回 1 个代码；full 返回该代码的行情
const payloadOf = (url) => {
  if (url.indexOf('/option/watchlist') === 0) return { ok: true, codes: [CODE] };
  if (url.indexOf('/option/full') === 0) {
    return { ok: true, data: [{ code: CODE, contract_name: '测试认购', last: 0.06, preclose: 0.05, change_pct: 20, delta: 0.3 }] };
  }
  return { ok: true, data: [], src: 'stub' };
};

(async () => {
  // ==================================================== A. 模板静态检查
  console.log('='.repeat(70));
  console.log('A. 模板静态检查 ' + FILE);
  console.log('='.repeat(70));
  {
    const h = read(FILE);
    check((h.match(/window\.addEventListener\('__forcerefresh', this\._onForce\);/g) || []).length === 1,
      '组件 mounted 注册 __forcerefresh 监听（1 处）');
    check((h.match(/window\.removeEventListener\('__forcerefresh', this\._onForce\);/g) || []).length === 1,
      '组件 beforeDestroy 摘除监听（1 处）');
    check((h.match(/^    _onForce\(\)\{/gm) || []).length === 1, '_onForce 方法定义唯一');
    check(h.indexOf('_onForce(){') < h.indexOf('_setupIO(){'), '_onForce 定义在 _setupIO 之前（methods 顺序正常）');

    // force 透传链
    check(h.indexOf('this._refresh(true); }') > 0, '_onForce：可见时立即 _refresh(true)');
    check(h.indexOf('else { this._forcePending = true; }') > 0, '_onForce：不可见时记账 _forcePending');
    check(/const f = this\._forcePending;\s*\/\//.test(h), '_setupIO：start() 里读取记账');
    check(h.indexOf('this._forcePending = false;') > 0, '_setupIO：记账消费后清零');
    check(h.indexOf('this._refresh(f);') > 0, '_setupIO：首绘用记账值（回到视野即强取）');
    check(h.indexOf("if(typeof IntersectionObserver === 'undefined'){ this._visible = true; start(); return; }") > 0,
      '无 IntersectionObserver 时也置 _visible=true（否则广播只会记账、永远没人消费）');

    check(h.indexOf('_refresh(force){') > 0 && h.indexOf('this._drawChart(force);') > 0, 'force 透传到 _drawChart');
    check(h.indexOf('_refreshLive(force){') > 0 && h.indexOf('this._loadQuote(force);') > 0, 'force 透传到 _loadQuote');
    check(h.indexOf('this._loadTicks(force);') > 0, 'force 透传到 _loadTicks');

    check(/minute\/' \+ encodeURIComponent\(this\.code\) \+ '\?ttl=' \+ \(force \? 0 : 10\)/.test(h),
      '分时：ttl = force ? 0 : 10');
    check(/quote\/' \+ encodeURIComponent\(this\.code\) \+ '\?ttl=' \+ \(force \? 0 : 3\)/.test(h),
      '盘口：ttl = force ? 0 : 3');
    check(/tick\/' \+ encodeURIComponent\(this\.code\) \+ '\?count=40&ttl=' \+ \(force \? 0 : 3\)/.test(h),
      '逐笔：ttl = force ? 0 : 3');
    check(h.indexOf("?ttl=10&_=") < 0 && h.indexOf("?ttl=3&_=") < 0, '已无写死的 ttl=10/3 URL');

    check(h.indexOf('@click="refreshAll()"') > 0, '工具栏「刷新」绑定 refreshAll()');
    check(h.indexOf('@click="reload(true)"') < 0, '已无「刷新直接 reload(true)、不广播」的旧绑定');
    check(h.indexOf('@click="refreshAll()"') < h.indexOf('@click="closeWin"'), '按钮顺序：刷新在关闭之前');

    check((h.match(/^    refreshAll\(\) \{/gm) || []).length === 1, 'refreshAll() 定义唯一');
    check(h.indexOf("window.dispatchEvent(new Event('__forcerefresh'));") > 0, 'refreshAll 广播 __forcerefresh');
    check((h.match(/^const FORCE_ON_LOAD = /gm) || []).length === 1, 'FORCE_ON_LOAD 定义唯一');
    check(h.indexOf("if (FORCE_ON_LOAD) { try { window.dispatchEvent(new Event('__forcerefresh')); } catch(e) {} }") > 0,
      'mounted：?force=1 时广播（综合窗口「刷新全部」入口）');
    check(h.indexOf('this.reload(true).then(() => this.applyRefreshInterval());') > 0, 'mounted 仍保留首屏 reload(true)');
  }

  // ==================================================== B. 可见格子：广播 → 立刻 ttl=0
  console.log('');
  console.log('='.repeat(70));
  console.log('B. 可见的格子收到广播 → 三个接口立刻 ttl=0，轮询不受污染');
  console.log('='.repeat(70));
  {
    const st = makeHarness(FILE, { search: '', payload: payloadOf, io: true });
    const c = buildComponent('opt-board-rt', st, PROPS);
    st.components['opt-board-rt'].mounted.call(c);
    check(st.io.observed() === 1, '格子已 observe 自身');
    check((st.listeners['__forcerefresh'] || []).length === 1, '已注册 1 个 __forcerefresh 监听');
    check(callsOf(st, MIN).length === 0, '未进视野时不取数（省流）');

    st.io.trigger(true);
    await drain(st);
    check(ttls3(st) === '10/3/3', '进视野首绘用默认 ttl 10/3/3（实际 ' + ttls3(st) + '）');
    check(c._visible === true, '_visible 已置 true');

    // 工具栏「刷新」广播（测试从外部派发，与真实按钮一致）
    const nMin = callsOf(st, MIN).length;
    st.window.dispatchEvent({ type: '__forcerefresh' });
    await drain(st);
    check(inDispatch(st), '广播已被派发到监听者');
    check(callsOf(st, MIN).length === nMin + 1, '广播后立刻取了一次分时（+1）');
    check(ttls3(st) === '0/0/0', '可见格子：三个接口全部 ttl=0（实际 ' + ttls3(st) + '）');
    check(c._forcePending !== true, '可见时不留 _forcePending（立即消费）');

    // 之后的 3s 轮询必须回到默认 ttl（force 只作用于那一次）
    fireInterval(st, 0);
    await drain(st);
    check(ttls3(st) === '10/3/3', '之后轮询回到 ttl 10/3/3（实际 ' + ttls3(st) + '）');
  }

  // ==================================================== C. 未进视野：记账 → 回视野首绘 ttl=0
  console.log('');
  console.log('='.repeat(70));
  console.log('C. 未进视野的格子：先记账，回到视野首绘即 ttl=0');
  console.log('='.repeat(70));
  {
    const st = makeHarness(FILE, { search: '', payload: payloadOf, io: true });
    const c = buildComponent('opt-board-rt', st, PROPS);
    st.components['opt-board-rt'].mounted.call(c);

    st.window.dispatchEvent({ type: '__forcerefresh' });
    await drain(st);
    check(c._forcePending === true, '滚出视野：记账 _forcePending=true');
    check(st.fetchUrls.filter(u => u.indexOf('/option/exquote/') === 0).length === 0,
      '记账期间一个行情请求都不发（省流）');

    st.io.trigger(true);
    await drain(st);
    check(ttls3(st) === '0/0/0', '回到视野首绘即 ttl=0（实际 ' + ttls3(st) + '）');
    check(c._forcePending === false, '_forcePending 已消费（一次性）');

    // 再广播一次不该“复活”旧记账；进视野后是可见态 → 立刻强取
    st.window.dispatchEvent({ type: '__forcerefresh' });
    await drain(st);
    check(ttls3(st) === '0/0/0', '可见状态下再广播仍是 ttl=0');
    check(c._forcePending === false, '不残留记账');

    // 滚出视野 → 轮询停止（clearInterval 真生效）
    st.io.trigger(false);
    const nBefore = callsOf(st, MIN).length;
    fireInterval(st, 0);
    await drain(st);
    check(callsOf(st, MIN).length === nBefore, '滚出视野后轮询停止（clearInterval 生效）');
    check(c._visible === false, '_visible 已置 false');
  }

  // ==================================================== D. 根实例 refreshAll
  console.log('');
  console.log('='.repeat(70));
  console.log('D. 根实例 refreshAll：广播 + 列表 force=1；轮询不带 force');
  console.log('='.repeat(70));
  {
    const st = await boot(FILE, { search: '', payload: payloadOf });
    const fullUrls = () => st.fetchUrls.filter(u => u.indexOf('/option/full') === 0);
    check(fullUrls().length >= 1, '首屏请求了 /option/full（' + fullUrls().length + ' 次）');
    check(fullUrls().every(u => u.indexOf('&force=1') > 0), '首屏 /option/full 带 force=1（保持原意图）');
    check(st.app.rows.length === 1 && String(st.app.rows[0].code) === CODE, '列表已按自选顺序聚合出 1 行');

    // 点工具栏「刷新」：先广播、再 reload(true)
    const nDisp = st.dispatched.length;
    const nFull = fullUrls().length;
    st.app.refreshAll();
    await drain(st);
    check(st.dispatched.length === nDisp + 1 && st.dispatched[st.dispatched.length - 1] === '__forcerefresh',
      'refreshAll 广播了 __forcerefresh');
    check(fullUrls().length === nFull + 1, 'refreshAll 同时重取了列表（+1）');
    check(fullUrls()[fullUrls().length - 1].indexOf('&force=1') > 0, '列表 URL 带 force=1');

    // 5s 轮询：不带 force（不能被首屏/刷新的 force 污染）
    const nIv = st.intervals.length;
    check(nIv === 1, '根实例只注册 1 个轮询（实测 ' + nIv + '）');
    fireInterval(st, 0);
    await drain(st);
    check(fullUrls().length === nFull + 2, '轮询再取一次列表');
    check(fullUrls()[fullUrls().length - 1].indexOf('force=1') < 0, '轮询 URL **不带** force（走缓存）');
    check(st.dispatched.length === nDisp + 1, '轮询不广播（只有点击刷新才广播）');

    // loading 中重复点击被忽略（不广播、不重取）
    st.app.loading = true;
    const nD2 = st.dispatched.length, nF2 = fullUrls().length;
    st.app.refreshAll();
    check(st.dispatched.length === nD2, 'loading 中点击不广播');
    check(fullUrls().length === nF2, 'loading 中点击不重取');
    st.app.loading = false;
  }

  // ==================================================== E. ?force=1（综合窗口入口）
  console.log('');
  console.log('='.repeat(70));
  console.log('E. ?force=1 时父 mounted 广播；不带 force 不广播');
  console.log('='.repeat(70));
  {
    const on = await boot(FILE, { search: '?force=1', payload: payloadOf });
    check(on.dispatched.indexOf('__forcerefresh') >= 0, '带 force=1 → mounted 广播 __forcerefresh');

    const off = await boot(FILE, { search: '?_t=1699000000000', payload: payloadOf });
    check(off.dispatched.indexOf('__forcerefresh') < 0, '无 force=1 → 不广播（独立开窗不额外穿透缓存）');
  }

  // ==================================================== F. 销毁后摘除监听
  console.log('');
  console.log('='.repeat(70));
  console.log('F. beforeDestroy 摘除监听');
  console.log('='.repeat(70));
  {
    const st = makeHarness(FILE, { search: '', payload: payloadOf, io: true });
    const c = buildComponent('opt-board-rt', st, PROPS);
    st.components['opt-board-rt'].mounted.call(c);
    st.io.trigger(true);
    await drain(st);
    check((st.listeners['__forcerefresh'] || []).length === 1, '销毁前监听在场');

    st.components['opt-board-rt'].beforeDestroy.call(c);
    check((st.listeners['__forcerefresh'] || []).length === 0, 'beforeDestroy 后监听已摘除');
    const n = st.fetchUrls.filter(u => u.indexOf('/option/exquote/') === 0).length;
    st.window.dispatchEvent({ type: '__forcerefresh' });
    await drain(st);
    check(st.fetchUrls.filter(u => u.indexOf('/option/exquote/') === 0).length === n,
      '销毁后广播不再引发请求（无内存泄漏式刷新）');
  }

  report();
})().catch(e => { console.error('测试自身异常：', e && e.stack || e); process.exit(2); });

function inDispatch(st) { return st.dispatched.indexOf('__forcerefresh') >= 0; }
