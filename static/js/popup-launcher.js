// 弹出独立窗口统一入口：检测后端返回 action 决定走「本机已起」还是「协议唤起 + 降级」。
//
// 用法（在按钮的 @click 处理器里调用）：
//   xtquantLaunchPopup({ url: '/option/popup/launch', title: '期权自选股看盘' });
//   xtquantLaunchPopup({ url: '/futures/popup/industry/stocks/launch',
//                        body: { board: '880362', name: '渔业' },
//                        title: '渔业' });
//
// 后端 launch 路由返回的两种 action：
//   { ok:true, action:'local_started',  url, title, w, h }  —— A 本机点按钮，后端已 Popen 弹窗，前端不再处理
//   { ok:true, action:'client_launch',  url, title, w, h }  —— B 局域网点按钮，前端尝试协议唤起，失败则 window.open 降级
//
// 自定义协议：xtquant-popup://?url=...&title=...&w=...&h=...
//   B 机器装了 popup_launcher.exe 并注册该协议 → 唤起 B 本地 PyWebView 窗口
//   没装 → 浏览器不会失焦，800ms 后降级 window.open(url)
//
// 依赖：本文件无任何依赖，浏览器原生即可。

(function (global) {
  'use strict';

  // 协议唤起后允许等待失焦的最大时长（毫秒）。超过则视为「未装客户端」走降级。
  var PROTOCOL_TIMEOUT_MS = 800;

  // 把 {a:1,b:'x y'} 编码成 URL query 字符串（空格→+，中文→%E4%B8...）
  function encodeQuery(obj) {
    if (!obj) return '';
    var parts = [];
    Object.keys(obj).forEach(function (k) {
      var v = obj[k];
      if (v === undefined || v === null) return;
      parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(String(v)));
    });
    return parts.join('&');
  }

  // 通过隐藏 <a> + 模拟点击触发协议。
  // 浏览器会先尝试调起已注册 xtquant-popup:// 协议的应用；
  // 调起成功 → 当前标签会进入后台（document.hidden=true / 触发 blur）；
  // 调起失败（无应用）→ 不会失焦，800ms 后由调用方降级。
  function tryProtocol(protocolUrl) {
    return new Promise(function (resolve) {
      var fired = false;
      var anchor = document.createElement('a');
      anchor.href = protocolUrl;
      anchor.style.display = 'none';
      anchor.rel = 'noopener';
      anchor.target = '_self';  // 不开新标签，让浏览器尝试协议唤起
      document.body.appendChild(anchor);

      function onBlur() {
        if (fired) return;
        fired = true;
        cleanup();
        resolve(true);  // 已唤起
      }
      function onVis() {
        if (document.hidden && !fired) {
          fired = true;
          cleanup();
          resolve(true);
        }
      }

      function cleanup() {
        window.removeEventListener('blur', onBlur, true);
        document.removeEventListener('visibilitychange', onVis);
        if (anchor && anchor.parentNode) anchor.parentNode.removeChild(anchor);
        if (timer) clearTimeout(timer);
      }

      // capture 阶段确保在应用层处理之前就能感知到
      window.addEventListener('blur', onBlur, true);
      document.addEventListener('visibilitychange', onVis);

      var timer = setTimeout(function () {
        if (fired) return;
        fired = true;
        cleanup();
        resolve(false);  // 超时未失焦 → 降级
      }, PROTOCOL_TIMEOUT_MS);

      // 用 MouseEvent 让 Chrome/Edge 视为「用户手势」允许协议唤起
      try {
        var ev = new MouseEvent('click', { bubbles: true, cancelable: true, view: window, button: 0 });
        anchor.dispatchEvent(ev);
      } catch (e) {
        anchor.click();
      }
    });
  }

  // 显示消息（优先用 Element-UI 的 $message，回退到 alert）
  function showMsg(win, type, text) {
    try {
      if (win && win.Vue && win.Vue.prototype && win.Vue.prototype.$message) {
        win.Vue.prototype.$message[type](text);
        return;
      }
    } catch (e) {}
    if (type === 'error') alert(text);
  }

  /**
   * 弹出独立窗口。
   * @param {Object} opts
   * @param {string} opts.url   后端 launch 路由（如 /option/popup/launch）
   * @param {Object} [opts.body]  POST body（默认空）
   * @param {string} [opts.title] 弹窗标题，仅用于成功消息显示
   * @param {Object} [opts.vm]    Vue 实例（用于 $message）
   * @param {Function} [opts.onStart] 启动前回调（一般用于设置 loading）
   * @param {Function} [opts.onEnd]   启动结束回调（无论成功失败）
   * @returns {Promise<boolean>}
   */
  function launchPopup(opts) {
    opts = opts || {};
    var postUrl = opts.url;
    var body = opts.body || {};
    var title = opts.title || '独立窗口';
    var vm = opts.vm || null;
    var onStart = opts.onStart || function () {};
    var onEnd = opts.onEnd || function () {};

    onStart();
    return fetch(postUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      cache: 'no-store',
      credentials: 'same-origin',
    })
      .then(function (r) { return r.json(); })
      .then(function (resp) {
        if (!resp || !resp.ok) {
          var err = (resp && resp.error) || '启动失败';
          showMsg(vm ? (vm.$root || vm) : window, 'error', title + '启动失败：' + err);
          return false;
        }
        if (resp.action === 'local_started') {
          // A 本机：后端已 Popen，弹窗在服务器桌面弹出（前端无需处理）
          showMsg(vm ? (vm.$root || vm) : window, 'success', title + '已在本机启动');
          return true;
        }
        if (resp.action === 'client_launch') {
          // B 局域网：尝试协议唤起 B 本地 PyWebView
          var protoUrl = 'xtquant-popup://?' + encodeQuery({
            url: resp.url,
            title: resp.title,
            w: resp.w,
            h: resp.h,
          });
          return tryProtocol(protoUrl).then(function (invoked) {
            if (invoked) {
              showMsg(vm ? (vm.$root || vm) : window, 'success', title + '已唤起本机弹窗');
              return true;
            }
            // 降级：浏览器新标签打开弹窗 URL
            try {
              window.open(resp.url, '_blank', 'noopener');
              showMsg(vm ? (vm.$root || vm) : window, 'success', title + '已在新标签打开');
            } catch (e) {
              showMsg(vm ? (vm.$root || vm) : window, 'error', title + '无法打开窗口：' + e.message);
              return false;
            }
            return true;
          });
        }
        showMsg(vm ? (vm.$root || vm) : window, 'error', title + '：未知 action=' + resp.action);
        return false;
      })
      .catch(function (err) {
        showMsg(vm ? (vm.$root || vm) : window, 'error', title + '启动失败：' + (err.message || err));
        return false;
      })
      .then(function (ok) { onEnd(); return ok; });
  }

  global.xtquantLaunchPopup = launchPopup;
})(window);
