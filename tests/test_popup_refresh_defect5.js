/* 缺口窗口补「刷新」—— 缺陷 5 回归测试
 *
 * 涉及三个窗口（审计报告 docs/POPUP_TTL_AUDIT.md 缺陷 5）：
 *   ① news_popup.html          新闻快讯：**整套工具栏 HTML 原本缺失**（只剩 CSS + toggleTop 死代码），
 *                              一并补齐「置顶 / 刷新 / 关闭」；fetchData 已在缺陷 2 时 force 化。
 *   ② industry_stocks_popup.html 个股分布：加按钮 + `&ttl=15` 写死 → `force ? 0 : 15`。
 *   ③ stock_chart_popup.html    个股分时：加按钮 + 分时/盘口/逐笔三处 force 化；
 *                              K 线原本**连 ttl 参数都不发**（恒用后端默认 120s 缓存）→ 前端显式传
 *                              `force ? 0 : 30`，后端 /market/kline 同步补上 ttl 透传。
 *
 * 断言要点：
 *   ① 三窗口都有 refreshBtn / :disabled="loading" / 顺序在「关闭」之前 / .btn:disabled 样式；
 *   ② 首屏与刷新都走 ttl=0，轮询走各自默认值（news 20 / istocks 15 / 分时 10 / K线 30 / 盘口 3）；
 *   ③ loading 中重复点击被忽略；失败路径 loading 复位（按钮不卡死）；
 *   ④ stock_chart 两视图分别正确，且「刷新」会把盘口/逐笔一并强刷（不重复请求）；
 *   ⑤ ECharts 窗口渲染后主动 resize（零尺寸兜底）。
 */
const fs = require('fs');
const path = require('path');
const H = require('C:/Users/zb/.workbuddy/skills/xtquant-popup-refresh/stub_test_harness.js');
const { check, makeHarness, boot, drain, fireInterval, ttlOf, report, setFile } = H;

const ROOT = 'D:/xtquant/templates';
const NEWS = 'news_popup.html';
const ISTK = 'industry_stocks_popup.html';
const SCHT = 'stock_chart_popup.html';

/* ------------------------------- 工具 ------------------------------- */
const urlsOf = (st, frag) => st.fetchUrls.filter(u => u.indexOf(frag) >= 0);
const countOf = (st, frag) => urlsOf(st, frag).length;
const lastTtl = (st, frag) => { const a = urlsOf(st, frag); return a.length ? ttlOf(a[a.length - 1]) : null; };

function htmlOf(f) { return fs.readFileSync(path.join(ROOT, f), 'utf8'); }

/** 三个窗口的公共静态检查 */
function staticChecks(f, label, extra) {
  console.log('\n' + '='.repeat(72));
  console.log('静态检查 · ' + label + ' (' + f + ')');
  console.log('='.repeat(72));
  const html = htmlOf(f);
  check(/id="refreshBtn"[^>]*@click="refresh"/.test(html), '有「刷新」按钮，绑定 refresh()');
  check(/:disabled="loading"/.test(html), '刷新按钮带 :disabled="loading"（期间置灰）');
  check(html.indexOf('id="refreshBtn"') < html.indexOf('关闭</button>'), '按钮顺序：刷新在「关闭」之前');
  check(html.indexOf('.btn:disabled {') > 0, '补了 .btn:disabled 样式');
  check(html.indexOf('refresh(){ if(this.loading) return; this.fetchData(true); }') > 0,
    'refresh() 有 loading 守卫并走 fetchData(true)');
  check(html.indexOf('ttl=15&') < 0 && html.indexOf('ttl=10&') < 0 && html.indexOf('ttl=3&') < 0,
    'URL 里已无写死的 &ttl=N（全部改走 force 三元式）');
  if (extra) extra(html);
}

/* ------------------------------- fetch 载荷 ------------------------------- */
function payloadNews() {
  return { ok: true, data: { ok: true, total: 1, data: [{
    code: '20260911001', showTime: '2026-09-11 10:00:00', title: '标题', summary: '摘要',
    titleColor: '1', stockList: ['平安银行'],
  }] } };
}
function payloadIstk() {
  return { ok: true, data: [
    { code: '000001', name: '平安银行', market: 0, price: 10.5, pct: 1.23, pre_close: 10.37 },
  ] };
}
function payloadScht(url) {
  if (url.indexOf('/market/minute/') >= 0) {
    return { ok: true, data: [[93000000, 10.5, 10.6, 10.4, 10.55, 100]] };
  }
  if (url.indexOf('/market/kline/') >= 0) {
    return { ok: true, data: [{ date: '2026-09-10', open: 10, high: 11, low: 9.5, close: 10.5, vol: 1000 }] };
  }
  if (url.indexOf('/market/depth/') >= 0) {
    return { ok: true, data: { asks: [[10.6, 100]], bids: [[10.4, 200]], price: 10.5, pre_close: 10.37 } };
  }
  if (url.indexOf('/market/tick/') >= 0) {
    return { ok: true, data: [{ time: '10:00:00', price: 10.5, vol: 100, buyorsell: 0 }] };
  }
  return { ok: true, data: {} };
}

(async () => {
  /* ================================================================== */
  /* 1. news_popup —— 补整套工具栏 + 刷新                                 */
  /* ================================================================== */
  setFile(NEWS);
  staticChecks(NEWS, '新闻快讯', html => {
    check(html.indexOf('<div class="tb-hover"></div>') > 0, '补齐悬停热区 .tb-hover（原缺失 → 工具栏永远显不出来）');
    check(html.indexOf('<div class="toolbar">') > 0, '补齐 .toolbar 容器（原只有 CSS 没有元素）');
    check(html.indexOf('7×24 快讯') > 0, '工具栏有标题');
    check(html.indexOf('closeWin(){ try{ window.close(); }catch(e){} }') > 0, '补了 closeWin（原本没有）');
    check(html.indexOf('toggleTop') > 0, '沿用既有 toggleTop');
    check(html.indexOf("(force ? 0 : 20)") > 0, 'fetchData 走 force ? 0 : 20（缺陷 2 时已 force 化）');
  });

  console.log('\n--- 新闻快讯 运行时 ---');
  {
    const st = await boot(NEWS, { payload: payloadNews });
    check(countOf(st, '/futures/news/data') === 1, '首屏只发 1 次快讯请求（实际 ' + countOf(st, '/futures/news/data') + '）');
    check(lastTtl(st, '/futures/news/data') === 0, '首屏 ttl=0（开窗即最新，实际 ' + lastTtl(st, '/futures/news/data') + '）');
    check(st.app.items.length === 1, '列表渲染出 1 条');
    check(st.app.updatedAt !== '', '更新时刻已填');
    check(st.intervals.length === 1 && st.intervals[0].delay === 60000, '1 个 60s 轮询（实际 ' +
      (st.intervals[0] && st.intervals[0].delay) + '）');

    // 轮询 → 默认 ttl
    const n0 = countOf(st, '/futures/news/data');
    fireInterval(st, 0); await drain(st);
    check(countOf(st, '/futures/news/data') === n0 + 1, '轮询 +1 次请求');
    check(lastTtl(st, '/futures/news/data') === 20, '轮询用 ttl=20（实际 ' + lastTtl(st, '/futures/news/data') + '）');

    // 刷新 → ttl=0
    st.app.refresh(); await drain(st);
    check(lastTtl(st, '/futures/news/data') === 0, 'refresh() → ttl=0（实际 ' + lastTtl(st, '/futures/news/data') + '）');
    check(st.app.loading === false, '刷新完成后 loading 复位');
    check(st.app.errMsg === '', '刷新成功无错误提示');

    // loading 中重复点击被忽略
    st.app.loading = true;
    const n1 = countOf(st, '/futures/news/data');
    st.app.refresh();
    check(countOf(st, '/futures/news/data') === n1, 'loading 中重复点击被忽略');
    st.app.loading = false;
  }
  { // 失败路径：loading 必须复位，否则按钮永久置灰
    const st = await boot(NEWS, { fetchMode: 'fail', payload: payloadNews });
    check(st.app.loading === false, '请求失败后 loading 复位（按钮不卡死）');
    check(/加载失败/.test(st.app.errMsg), '失败给出提示（' + st.app.errMsg + '）');
  }

  /* ================================================================== */
  /* 2. industry_stocks_popup —— 加按钮 + ttl 写死 15 改力                            */
  /* ================================================================== */
  setFile(ISTK);
  staticChecks(ISTK, '个股分布', html => {
    check(html.indexOf("'&ttl=' + (force ? 0 : 15)") > 0, 'URL 改成 force ? 0 : 15（原写死 &ttl=15）');
    check(html.indexOf('function chartFor') < 0 || true, '(占位)');
  });

  console.log('\n--- 个股分布 运行时 ---');
  {
    const st = await boot(ISTK, {
      payload: payloadIstk,
      dataset: { board: 'BK0475', name: '银行' },
    });
    check(st.app.board === 'BK0475' && st.app.boardName === '银行', '从 data-board / data-name 读到初始化参数');
    check(countOf(st, '/futures/industry/stocks/data') === 1, '首屏发 1 次取数');
    check(lastTtl(st, '/futures/industry/stocks/data') === 0, '首屏 ttl=0（实际 ' + lastTtl(st, '/futures/industry/stocks/data') + '）');
    check(urlsOf(st, '/futures/industry/stocks/data')[0].indexOf('board=BK0475') > 0, 'URL 带上 board 参数');
    check(st.app.list.length === 1, '拿到 1 只成分股');
    check(st.init === 1 && st.setOption === 1, 'ECharts 初始化 1 次、setOption 1 次');
    check(st.resize >= 1, '渲染后主动 resize（零尺寸兜底，实际 ' + st.resize + '）');

    // 轮询 → 15
    const n0 = countOf(st, '/futures/industry/stocks/data');
    const rz0 = st.resize;
    fireInterval(st, 0); await drain(st);
    check(countOf(st, '/futures/industry/stocks/data') === n0 + 1, '轮询 +1 次请求');
    check(lastTtl(st, '/futures/industry/stocks/data') === 15, '轮询用 ttl=15（实际 ' + lastTtl(st, '/futures/industry/stocks/data') + '）');
    check(st.resize > rz0, '轮询重绘后同样 resize');

    // 刷新 → 0
    st.app.refresh(); await drain(st);
    check(lastTtl(st, '/futures/industry/stocks/data') === 0, 'refresh() → ttl=0（实际 ' + lastTtl(st, '/futures/industry/stocks/data') + '）');
    check(st.app.loading === false, '刷新完成后 loading 复位');
    check(st.app.updatedAt.indexOf('加载失败') < 0, '刷新成功（updatedAt=' + st.app.updatedAt + '）');

    // loading 中忽略
    st.app.loading = true;
    const n1 = countOf(st, '/futures/industry/stocks/data');
    st.app.refresh();
    check(countOf(st, '/futures/industry/stocks/data') === n1, 'loading 中重复点击被忽略');
    st.app.loading = false;
  }
  { // 缺 board 参数的兜底
    const st = await boot(ISTK, { payload: payloadIstk, dataset: {} });
    check(countOf(st, '/futures/industry/stocks/data') === 0, '缺 board 时不发请求');
    check(st.app.updatedAt === '缺少板块参数', '缺 board 时给出说明（' + st.app.updatedAt + '）');
  }
  { // 失败路径
    const st = await boot(ISTK, { fetchMode: 'fail', payload: payloadIstk, dataset: { board: 'BK0475', name: '银行' } });
    check(st.app.loading === false, '失败后 loading 复位');
    check(/加载失败/.test(st.app.updatedAt), '失败给出提示（' + st.app.updatedAt + '）');
  }

  /* ================================================================== */
  /* 3. stock_chart_popup —— 加按钮 + 分时/盘口/逐笔/K线 force 化          */
  /* ================================================================== */
  setFile(SCHT);
  staticChecks(SCHT, '个股分时', html => {
    check(html.indexOf("(force ? 0 : 10)") > 0, '分时 URL 改 force ? 0 : 10');
    check(html.indexOf("&ttl=' + (force ? 0 : 30)") > 0, 'K线 URL 新增 ttl=force ? 0 : 30（原来连 ttl 都不发）');
    check(html.indexOf("(force ? 0 : 3)") > 0, '盘口/逐笔改 force ? 0 : 3');
    check(html.indexOf('fetchDepth(force){') > 0, 'fetchDepth 接住 force');
    check(html.indexOf('this.fetchDepth(force);') > 0, 'fetchData 内把 force 透传给 fetchDepth');
    check(html.indexOf('if(this.chart.resize) this.chart.resize();') > 0, 'render 末尾补 resize');
  });

  console.log('\n--- 个股分时 运行时（分时视图）---');
  {
    const ds = { code: '000001', market: '0', name: '平安银行', pc: '10.37' };
    const st = await boot(SCHT, { payload: payloadScht, dataset: ds });
    check(st.app.codeFull === '000001.SZ', 'codeFull 由 code+market 拼出（' + st.app.codeFull + '）');
    check(st.app.view === 'rt' && st.app.showDepth === false, '默认分时视图、盘口关闭');
    check(countOf(st, '/market/minute/') === 1, '首屏取分时 1 次');
    check(lastTtl(st, '/market/minute/') === 0, '首屏分时 ttl=0（实际 ' + lastTtl(st, '/market/minute/') + '）');
    check(countOf(st, '/market/depth/') === 0 && countOf(st, '/market/tick/') === 0, '盘口关闭时不取盘口/逐笔');
    check(st.resize >= 1, '渲染后 resize（实际 ' + st.resize + '）');

    // 轮询 → ttl=10
    const n0 = countOf(st, '/market/minute/');
    fireInterval(st, 0); await drain(st);
    check(countOf(st, '/market/minute/') === n0 + 1, '轮询 +1 次分时请求');
    check(lastTtl(st, '/market/minute/') === 10, '轮询分时 ttl=10（实际 ' + lastTtl(st, '/market/minute/') + '）');

    // 打开盘口 → 默认节流 ttl=3
    st.app.toggleDepth(); await drain(st);
    check(st.app.showDepth === true, '盘口已打开');
    check(lastTtl(st, '/market/depth/') === 3, '打开盘口时走默认 ttl=3（实际 ' + lastTtl(st, '/market/depth/') + '）');
    check(lastTtl(st, '/market/tick/') === 3, '逐笔走默认 ttl=3（实际 ' + lastTtl(st, '/market/tick/') + '）');

    // 刷新 → 分时 + 盘口 + 逐笔 全部 ttl=0，且各只 +1（不重复请求）
    const a = countOf(st, '/market/minute/'), b = countOf(st, '/market/depth/'), c = countOf(st, '/market/tick/');
    st.app.refresh(); await drain(st);
    check(countOf(st, '/market/minute/') === a + 1 && lastTtl(st, '/market/minute/') === 0, 'refresh：分时 +1 且 ttl=0');
    check(countOf(st, '/market/depth/') === b + 1 && lastTtl(st, '/market/depth/') === 0, 'refresh：盘口 +1 且 ttl=0（未重复请求）');
    check(countOf(st, '/market/tick/') === c + 1 && lastTtl(st, '/market/tick/') === 0, 'refresh：逐笔 +1 且 ttl=0（未重复请求）');
    check(st.app.loading === false, '刷新完成后 loading 复位');

    // loading 中忽略
    st.app.loading = true;
    const a2 = countOf(st, '/market/minute/');
    st.app.refresh();
    check(countOf(st, '/market/minute/') === a2, 'loading 中重复点击被忽略');
    st.app.loading = false;
  }

  console.log('\n--- 个股分时 运行时（K线视图）---');
  {
    const st = await boot(SCHT, {
      payload: payloadScht,
      dataset: { code: '000001', market: '0', name: '平安银行', pc: '10.37' },
    });
    st.app.switchView('kl'); await drain(st);
    check(countOf(st, '/market/kline/') === 1, '切 K线后取 1 次');
    check(lastTtl(st, '/market/kline/') === 0, '切换视图本就强制 → ttl=0（实际 ' + lastTtl(st, '/market/kline/') + '）');
    check(st.app.fullList.length === 1 && st.app.list.length === 1, 'K线数据落到 fullList / list');
    check(countOf(st, '/market/minute/') === 1, '切视图后不再取分时（仍只有首屏那次）');

    // 轮询 → ttl=30
    const n0 = countOf(st, '/market/kline/');
    fireInterval(st, 0); await drain(st);
    check(countOf(st, '/market/kline/') === n0 + 1, 'K线视图轮询 +1 次');
    check(lastTtl(st, '/market/kline/') === 30, 'K线轮询 ttl=30（实际 ' + lastTtl(st, '/market/kline/') + '）');

    // 刷新 → 0，且不涉及盘口
    st.app.refresh(); await drain(st);
    check(lastTtl(st, '/market/kline/') === 0, 'refresh：K线 ttl=0（实际 ' + lastTtl(st, '/market/kline/') + '）');
    check(countOf(st, '/market/depth/') === 0, 'K线视图刷新不取盘口');

    // 切周期（用户主动操作）也应强制
    st.app.period = '5m'; st.app.onPeriod(); await drain(st);
    check(lastTtl(st, '/market/kline/') === 0, '切周期 ttl=0（实际 ' + lastTtl(st, '/market/kline/') + '）');
  }

  report();
})();
