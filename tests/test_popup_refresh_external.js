/* 外围股市（external_popup.html）自动刷新节奏 —— 缺陷 4 回归测试
 *
 * 缺陷：原先 created() 里 `setInterval(refreshAll, 5000)` —— 每 5s 重挂全部 8 张外盘图 URL，
 *       等于每分钟重下 12 轮整批图（外盘图是分钟级数据，纯浪费），且每轮都把失败图的重试态清零。
 * 修法：改为「对齐下一分钟边界 + 2s 宽限」的递归 setTimeout；手动刷新不参与该调度。
 *
 * 断言要点：
 *   ① 没有任何 setInterval；created 只排一个定时器；
 *   ② 无论何时 created，到期时刻都落在「分钟边界 + 2s」（delay ∈ [2000, 62000]）；
 *   ③ 同样虚拟 5 分钟，自动刷新 5 次（旧实现 5s 定频会是 60 次，12 倍）；
 *   ④ 手动 refresh 不打乱已排的自动调度；
 *   ⑤ beforeDestroy 用 clearTimeout 真停掉（不是 clearInterval）；
 *   ⑥ 失败图重试上限 3 次后置 failed，手动刷新可恢复。
 */
const fs = require('fs');
const path = require('path');
const H = require('C:/Users/zb/.workbuddy/skills/xtquant-popup-refresh/stub_test_harness.js');
const { check, makeHarness, drain, report } = H;

const FILE = 'external_popup.html';
const ROOT = 'D:/xtquant/templates';
const PERIOD = 60000, GRACE = 2000;
H.setFile(FILE);

/** 新建一个「独立打开」的外盘窗口，并跑完 created（now 为可注入墙钟） */
function open(now) {
  const st = makeHarness(FILE, { now: now });
  st.options.created.call(st.app);
  return st;
}

/** src 快照，用于判断「URL 是否真被重挂」 */
const srcs = (st) => st.app.items.map(it => it.src);

(async () => {
  // ==================================================== 0. 模板静态检查
  console.log('='.repeat(70));
  console.log('0. 模板静态检查');
  console.log('='.repeat(70));
  {
    const html = fs.readFileSync(path.join(ROOT, FILE), 'utf8');
    check(html.indexOf('setInterval(') < 0, '已无 setInterval 调用（定频轮询已移除）');
    check(html.indexOf('clearInterval(') < 0, '已无 clearInterval 调用');
    check((html.match(/scheduleNextRefresh/g) || []).length === 3,
      'scheduleNextRefresh 出现 3 次（created 调用 / 定义 / 递归自排）');
    check(html.indexOf('clearTimeout(this.timer)') > 0, 'beforeDestroy 用 clearTimeout 取消（+ scheduleNextRefresh 内先取消）');
    check(/id="refreshBtn"[^>]*@click="refresh"/.test(html), '工具栏仍有「刷新」按钮，绑定 refresh()');
    check(html.indexOf(':disabled="refreshing"') > 0, '刷新按钮带 :disabled="refreshing"（期间置灰）');
    check(html.indexOf("Math.random()") > 0, 'buildSrc 仍带 _random（绕浏览器缓存）');
    check(html.indexOf('unpkg.com') < 0, '无 unpkg 外网依赖');
    check(html.indexOf("var PERIOD = 60000") > 0 && html.indexOf("var GRACE = 2000") > 0,
      '调度常量 PERIOD=60000 / GRACE=2000 就位');
  }

  // ==================================================== 1. created 只排一次、且对齐分钟边界
  console.log('\n' + '='.repeat(70));
  console.log('1. created 只排一次调度，到期时刻对齐「分钟边界 + 2s」');
  console.log('='.repeat(70));
  {
    const st = open(0);                       // 墙钟 = 0，即正好压在某分钟边界上
    check(st.intervals.length === 0, '没有任何 setInterval');
    check(st.timers.length === 1, 'created 只排了 1 个定时器（实际 ' + st.timers.length + '）');
    check(st.timers[0].delay === 62000, '边界处创建 → delay=62000（实际 ' + st.timers[0].delay + '）');
    check(st.app.updatedAt === '', '首屏不刷新（图片由模板自身 src 直接加载）');
    check(st.app.items.length === 8, '8 张外盘图（实际 ' + st.app.items.length + '）');
    check(st.app.items.every(it => it.src.indexOf('/futures/external/img?nid=') === 0),
      '每张图 URL 都走本机代理（绕开 WebView 直连外网）');
  }

  // ==================================================== 2. 任意时刻创建都落在边界 +2s
  console.log('\n' + '='.repeat(70));
  console.log('2. 窗口在任意秒打开，下次刷新都落在「下一分钟 +2s」');
  console.log('='.repeat(70));
  [0, 1, 999, 30000, 45000, 59999].forEach(function (off) {
    const st = open(off);
    const d = st.timers[0].delay;
    check(d >= GRACE && d <= PERIOD + GRACE,
      '偏移 ' + off + 'ms → delay=' + d + ' 落在 [2000, 62000]');
    check((off + d) % PERIOD === GRACE,
      '偏移 ' + off + 'ms → 到期时刻 ' + (off + d) + ' 满足「边界+2s」');
  });

  // ==================================================== 3. 节奏：5 分钟 5 次（旧实现 60 次）
  console.log('\n' + '='.repeat(70));
  console.log('3. 自动刷新节奏（虚拟时钟推进 5 分钟）');
  console.log('='.repeat(70));
  {
    const st = open(0);
    st.nowMs = 62000;                         // 模拟真实时间也走到第一个到期点
    await drain(st, 62000);                   // 触发第 1 次自动刷新
    check(st.app.updatedAt !== '', '到点自动刷新了一次（updatedAt 已写）');
    check(st.timers.length === 1, '刷新后自动排下一次（实际 ' + st.timers.length + '）');
    check(st.timers[0].delay === 60000, '下一次 delay=60000（整分钟，实际 ' + st.timers[0].delay + '）');

    // 计数：包裹 refreshAll（scheduleNextRefresh 里是 this.refreshAll()，运行时查表 → 包裹生效）
    let n = 0;
    const real = st.app.refreshAll;
    st.app.refreshAll = function () { n++; return real.apply(this, arguments); };

    let prev = srcs(st);
    for (let m = 0; m < 5; m++) {
      st.nowMs += 60000;                      // 真实时间过 1 分钟
      await drain(st, 60000);                 // 虚拟时钟推到下一个到期点
      const now = srcs(st);
      check(now.every((s, i) => s !== prev[i]), '第 ' + (m + 1) + ' 分钟：8 张图 URL 全部重挂（换新 _random）');
      check(st.timers.length === 1 && st.timers[0].delay === 60000,
        '第 ' + (m + 1) + ' 分钟：仍只排 1 个、仍对齐 60000');
      prev = now;
    }
    check(n === 5, '虚拟 5 分钟内自动刷新 ' + n + ' 次，应为 5（旧 5s 定频会是 60 次，降 12 倍）');
    check(st.intervals.length === 0, '全程没有 setInterval 兜底轮询');
  }

  // ==================================================== 4. 手动刷新不打乱自动调度
  console.log('\n' + '='.repeat(70));
  console.log('4. 手动刷新与自动调度互不干扰');
  console.log('='.repeat(70));
  {
    const st = open(0);
    const atBefore = st.timers[0].at;
    const before = srcs(st);
    st.app.refresh();
    check(st.app.refreshing === true, '手动刷新置 refreshing=true（按钮置灰）');
    check(srcs(st).every((s, i) => s !== before[i]), '手动刷新同样重挂全部图片 URL');
    check(st.timers.filter(t => t.delay >= PERIOD - GRACE).length === 1,
      '手动刷新没有新增/替换自动调度（仍只有 1 个分钟级定时器）');
    const auto = st.timers.find(t => t.delay >= PERIOD - GRACE);
    check(auto.at === atBefore, '自动调度的到期时刻未被手动刷新改动（' + auto.at + ' == ' + atBefore + '）');

    await drain(st, 700);                     // 600ms 置灰复位
    check(st.app.refreshing === false, '600ms 后 refreshing 复位');
    check(st.timers.find(t => t.delay >= PERIOD - GRACE).at === atBefore,
      '复位后自动调度依然没被带偏');
  }

  // ==================================================== 5. beforeDestroy 真停掉
  console.log('\n' + '='.repeat(70));
  console.log('5. beforeDestroy 用 clearTimeout 真停掉调度（窗口关闭后不再无谓下载）');
  console.log('='.repeat(70));
  {
    const st = open(0);
    check(st.timers.length === 1, '销毁前有 1 个待触发的自动调度');
    st.options.beforeDestroy.call(st.app);
    check(st.app.timer === null, 'beforeDestroy 把 timer 置空');
    await drain(st);                          // 一路推进；被 clearTimeout 的定时器不应触发
    check(st.app.updatedAt === '', '销毁后推进时钟：没有发生任何自动刷新');
    check(st.timers.length === 0, '销毁后没有残留定时器（实际 ' + st.timers.length + '）');
  }

  // ==================================================== 6. 失败图重试与恢复
  console.log('\n' + '='.repeat(70));
  console.log('6. 图片失败重试：上限 3 次置 failed，手动刷新可恢复');
  console.log('='.repeat(70));
  {
    const st = open(0);
    const it = st.app.items[0];
    for (let r = 1; r <= 3; r++) {
      const before = it.src;
      st.app.onImgError(it);
      check(it.retry === r, '第 ' + r + ' 次失败 → retry=' + r);
      check(it.failed === false, '第 ' + r + ' 次失败：仍在重试中（未置 failed）');
      await drain(st, 1000 * r);              // 退避 1s/2s/3s
      check(it.src !== before, '第 ' + r + ' 次失败：退避 ' + r + 's 后重挂 URL');
    }
    st.app.onImgError(it);
    check(it.retry === 4 && it.failed === true, '第 4 次失败 → retry=4 置 failed（不再无脑重试）');

    st.app.refresh();                         // 手动刷新 = 用户主动重试
    check(it.failed === false && it.retry === 0, '手动刷新把 failed/retry 清零（可恢复）');
  }

  report();
})();
