/* 综合多面板窗口「刷新全部」透传 force 的回归测试（缺陷 2）
 *
 * 背景：`integrated_popup.html` 是 **iframe 容器**，自身不取数。「刷新全部」的实现是
 *   重挂 `iframe.src = base + '?_t=' + Date.now()` —— 但 `?_t=` **只绕过浏览器缓存**；
 *   子页重新加载后走的是**首屏默认 ttl**（amountflow 60 / zhangfu 30 / industry 30 / news 20）
 *   → **命中服务端缓存** → 看起来重绘了、画的还是旧数据。
 *
 * 修法：容器重挂时追加 `&force=1`；各子页首屏读它 → `fetchData(true)`（ttl=0）。
 *
 * 断言要点：
 *   A. 容器：8 个 iframe 全部追加 force=1，且反复刷新不会累积重复参数；
 *   B. amountflow / zhangfu / industry：带 force=1 → 首屏 ttl=0；不带 → 默认 ttl（轮询仍用默认）；
 *   C. news：首屏本就是 force（意图保留）→ ttl=0；轮询维持 ttl=20；
 *   D. futures：带 force=1 时 mounted 广播 __forcerefresh；子组件若还没进视野会先记 _forcePending，
 *      进入视野那次绘制就必须是 ttl=0（这条同时验证了「子组件 mounted 先于父组件」的顺序假设）；
 *   E. option：首屏本来就 fetchData(true) → force=1，无需改造。
 */
const fs = require('fs');
const path = require('path');
const H = require('C:/Users/zb/.workbuddy/skills/xtquant-popup-refresh/stub_test_harness.js');
const { check, makeHarness, buildComponent, boot, drain, ttlOf, fireInterval, report } = H;

const ROOT = 'D:/xtquant/templates';
const read = (f) => fs.readFileSync(path.join(ROOT, f), 'utf8');

const payload = (url) => ({
  ok: true,
  data: {
    ok: true, data: [], list: [], text: '', total: 0,
    up: 1, down: 1, flat: 0, pool: 2,
    bar: { series: [] }, line: { series: [] }, series: [],
  },
});
const payloadOf = payload;

const callsOf = (st, prefix) => st.fetchUrls.filter(u => u.indexOf(prefix) === 0);
const lastTtlOf = (st, prefix) => { const a = callsOf(st, prefix); return a.length ? ttlOf(a[a.length - 1]) : null; };

(async () => {
  // ==================================================== 0. 模板静态检查
  console.log('='.repeat(70));
  console.log('0. 模板静态检查（7 个文件）');
  console.log('='.repeat(70));
  {
    const c = read('integrated_popup.html');
    check(c.indexOf('.split(\'?_t=\')[0]') > 0, '容器：仍以 ?_t= 为界剥离旧 query（避免参数累积）');
    check(/'&force=1'/.test(c), '容器：重挂 URL 追加 &force=1');
    check(c.indexOf("iframe.src = base + '?_t=' + (Date.now() + i);") < 0, '容器：已无「只挂 _t、不带 force」的旧写法');
    check((c.match(/&force=1/g) || []).length === 1, "容器：'&force=1' 只出现一次（避免重复拼接）");

    for (const f of ['amountflow_popup.html', 'zhangfu_popup.html', 'industry_popup.html']) {
      const h = read(f);
      check((h.match(/const FORCE_ON_LOAD = /g) || []).length === 1, f + '：FORCE_ON_LOAD 定义唯一');
      check(h.indexOf('fetchData(FORCE_ON_LOAD)') > 0, f + '：首屏用 FORCE_ON_LOAD');
      check((h.match(/fetchData\(false\)/g) || []).length === 1, f + '：fetchData(false) 仅剩轮询一处');
      check(h.indexOf('location.search') > 0, f + '：读取 location.search');
    }

    const n = read('news_popup.html');
    check(/ttl=' \+ \(force \? 0 : 20\)/.test(n), 'news：force 真正映射为 ttl=0/20（原来写死 20）');
    check(n.indexOf("'/futures/news/data?ttl=20&") < 0, 'news：已无写死 ttl=20 的 URL');
    check(n.indexOf('this.fetchData(true);') > 0, 'news：首屏仍是 force=true（保持原意图）');

    const fu = read('futures_popup.html');
    check((fu.match(/const FORCE_ON_LOAD = /g) || []).length === 1, 'futures：FORCE_ON_LOAD 定义唯一');
    check(fu.indexOf("window.dispatchEvent(new Event('__forcerefresh'))") > 0, 'futures：mounted 里广播 __forcerefresh');
    check(/if \(FORCE_ON_LOAD\) \{ try \{ window\.dispatchEvent/.test(fu), 'futures：广播受 FORCE_ON_LOAD 保护');

    const o = read('option.html');
    check(o.indexOf('this.fetchData(true).then(() => this.applyRefreshInterval())') > 0,
      'option：首屏本就 fetchData(true) → 自带 force=1，无需改造');
    check(o.indexOf('FORCE_ON_LOAD') < 0, 'option：未引入多余开关（列表模式无格子）');
  }

  // ==================================================== A. 容器：追加 force=1
  console.log('');
  console.log('='.repeat(70));
  console.log('A. 容器 refreshAll 给每个 iframe 追加 force=1');
  console.log('='.repeat(70));
  {
    const st = await boot('integrated_popup.html', { search: '', payload: payloadOf });
    const bases = [
      '/futures/amountflow/popup', '/futures/zhangfu/popup', '/futures/capital/popup',
      '/futures/industry/popup', '/futures/external/popup', '/futures/popup',
      '/futures/news/popup', '/option/popup',
    ];
    const frames = bases.map(b => ({ src: 'http://127.0.0.1:5000' + b + '?_t=1699000000000' }));
    st.queryAll = { '.frame-wrap iframe': frames };

    st.app.refreshAll();
    const allForce = frames.every(f => f.src.indexOf('&force=1') > 0);
    check(frames.length === 8, '容器共 8 个 iframe（实测 ' + frames.length + '）');
    check(allForce, '8 个 iframe 全部带上 force=1');
    check(frames.every(f => f.src.indexOf('?_t=') > 0), '8 个 iframe 仍带 _t=（继续绕浏览器缓存）');
    check(frames.every(f => (f.src.match(/\?/g) || []).length === 1), '每个 URL 只有 1 个 ?（参数拼接正确）');
    check(frames.every(f => (f.src.match(/force=1/g) || []).length === 1), '每个 URL force=1 恰好 1 次');
    check(frames.every((f, i) => f.src.indexOf('http://127.0.0.1:5000' + bases[i]) === 0), '各 iframe 的路径未被改坏');
    check(frames.every((f, i) => f.src !== 'http://127.0.0.1:5000' + bases[i] + '?_t=1699000000000'),
      '所有 src 都真的被改成新 URL（不是原地不动）');

    // 连点两次不应累积 ?_t= / &force=1
    st.app.refreshAll();
    check(frames.every(f => (f.src.match(/force=1/g) || []).length === 1), '二次刷新后 force=1 仍只有 1 个（不累积）');
    check(frames.every(f => (f.src.match(/[?&]_t=/g) || []).length === 1), '二次刷新后 _t= 仍只有 1 个（不累积）');
    check(/^刷新全部 \d/.test(st.app.updatedAt || ''), '刷新后 updatedAt 给出反馈 (' + st.app.updatedAt + ')');
  }

  // ==================================================== B. 三个 Vue 子页首屏 ttl
  console.log('');
  console.log('='.repeat(70));
  console.log('B. amountflow / zhangfu / industry：force=1 → 首屏 ttl=0');
  console.log('='.repeat(70));
  const cases = [
    { file: 'amountflow_popup.html', prefix: '/futures/amountflow/data', def: 60, name: '两市成交' },
    { file: 'zhangfu_popup.html', prefix: '/futures/zhangfu/distribution', def: 30, name: '涨幅分布' },
    { file: 'industry_popup.html', prefix: '/futures/popup/industry', def: 30, name: '行业分布' },
    { file: 'news_popup.html', prefix: '/futures/news/data', def: 20, name: '新闻快讯' },
  ];
  for (const cs of cases) {
    const forced = await boot(cs.file, { search: '?force=1', payload: payloadOf, width: 900 });
    const plain = await boot(cs.file, { search: '', payload: payloadOf, width: 900 });
    const ft = lastTtlOf(forced, cs.prefix);
    const pt = lastTtlOf(plain, cs.prefix);
    // news 首屏本来就是 force=true（意图保留）→ 两种入口都应是 0
    const expectPlain = (cs.file === 'news_popup.html') ? 0 : cs.def;
    check(ft === 0, cs.name + '：带 force=1 首屏 ttl=0（实际 ' + ft + '）');
    check(pt === expectPlain, cs.name + '：不带 force 首屏 ttl=' + expectPlain + '（实际 ' + pt + '）');
    check(forced.fetchUrls.length >= 1, cs.name + '：首屏确实发出了请求 (' + forced.fetchUrls.length + ')');

    // 轮询必须维持默认 ttl（不能被首屏的 force 污染）
    fireInterval(forced, 0);
    await drain(forced);
    const pollTtl = lastTtlOf(forced, cs.prefix);
    check(pollTtl === cs.def, cs.name + '：轮询维持 ttl=' + cs.def + '（实际 ' + pollTtl + '）');
  }
  {
    // news 轮询也不能被写死值污染
    const st = await boot('news_popup.html', { search: '', payload: payloadOf });
    fireInterval(st, 0);
    await drain(st);
    check(lastTtlOf(st, '/futures/news/data') === 20, 'news：轮询 ttl=20（force 只作用于首屏）');
  }

  // ==================================================== D. futures：广播给 16 个格子
  console.log('');
  console.log('='.repeat(70));
  console.log('D. futures：force=1 时广播 __forcerefresh，格子首绘 ttl=0');
  console.log('='.repeat(70));
  {
    const FILE = 'futures_popup.html';
    const PROPS = { code: 'IF2609', prevClose: 3980, price: 4000, chgPct: 0.5 };

    // D-1 「不带 force」：不应广播，格子首绘维持 ttl=3
    {
      const st = makeHarness(FILE, { search: '', payload: payloadOf, io: true });
      const c = buildComponent('fut-compact-rt', st, PROPS);
      st.components['fut-compact-rt'].mounted.call(c);      // 格子先注册监听（真实顺序：子先于父）
      st.options.mounted.call(st.app);                      // 父 mounted
      await drain(st);
      check(st.dispatched.indexOf('__forcerefresh') < 0, '不带 force 时**不**广播 __forcerefresh');
      check(callsOf(st, '/futures/exquote/minute').length === 0, '未进视野时不取数（省流）');
      st.io.trigger(true);
      await drain(st);
      check(lastTtlOf(st, '/futures/exquote/minute') === 3, '不带 force 首绘 ttl=3（实际 ' + lastTtlOf(st, '/futures/exquote/minute') + '）');
    }

    // D-2 「带 force」：广播 → 两个组件都未进视野时先记账 → 进视野首绘 ttl=0
    {
      const st = makeHarness(FILE, { search: '?force=1', payload: payloadOf, io: true });
      const cRt = buildComponent('fut-compact-rt', st, PROPS);
      const cKl = buildComponent('fut-kline', st, PROPS);
      // 真实生命周期顺序：子组件 mounted 先于父组件 mounted（Vue 2），所以此刻监听已就绪
      st.components['fut-compact-rt'].mounted.call(cRt);
      st.components['fut-kline'].mounted.call(cKl);
      check(st.io.observed() === 2, '两个格子都已 observe 自身（' + st.io.observed() + '）');
      st.options.mounted.call(st.app);
      await drain(st);
      check(st.dispatched.indexOf('__forcerefresh') >= 0, '带 force=1 时广播了 __forcerefresh');
      check(callsOf(st, '/futures/exquote/minute').length === 0, '广播时未进视野 → 不立刻取数（省流）');
      check(cRt._forcePending === true, '分时格子记账 _forcePending=true（回到视野再强取）');
      check(cKl._forcePending === true, 'K线格子记账 _forcePending=true');

      st.io.trigger(true);
      await drain(st);
      check(lastTtlOf(st, '/futures/exquote/minute') === 0, '分时格子进视野首绘即 ttl=0（实际 ' + lastTtlOf(st, '/futures/exquote/minute') + '）');
      check(lastTtlOf(st, '/futures/exquote/kline') === 0, 'K线格子进视野首绘即 ttl=0（实际 ' + lastTtlOf(st, '/futures/exquote/kline') + '）');
      check(cRt._forcePending === false && cKl._forcePending === false, '_forcePending 均已消费（一次性）');

      // 之后轮询仍维持各自的默认 ttl（分时 3 / K线 15），不被 force 污染。
      // 注意 interval 下标：根实例 mounted 里的 fetchSnapshot().then(applyRefreshInterval)
      // 已经注册了 1 个 5s 快照轮询，所以两个格子的轮询在末尾两位。
      const nIv = st.intervals.length;
      check(nIv === 3, '此时共 3 个轮询：根快照(5s) + 分时(3s) + K线(3s)（实测 ' + nIv + '）');
      fireInterval(st, nIv - 2);
      await drain(st);
      check(lastTtlOf(st, '/futures/exquote/minute') === 3, '分时轮询维持 ttl=3（实际 ' + lastTtlOf(st, '/futures/exquote/minute') + '）');
      fireInterval(st, nIv - 1);
      await drain(st);
      check(lastTtlOf(st, '/futures/exquote/kline') === 15, 'K线轮询维持 ttl=15（实际 ' + lastTtlOf(st, '/futures/exquote/kline') + '）');
    }
  }

  report();
})().catch(e => { console.error('测试自身异常：', e && e.stack || e); process.exit(2); });
