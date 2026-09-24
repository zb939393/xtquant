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
// 调试：F12 控制台看 [xtq-popup] 前缀的日志；任何错误都会 alert 弹窗（B 用户能看到）。

(function (global) {
  'use strict';

  // 协议唤起后允许等待失焦的最大时长（毫秒）。超过则视为「未装客户端」走降级。
  var PROTOCOL_TIMEOUT_MS = 800;

  // ---- 调试工具 ----
  function log() {
    if (global.console && global.console.log) {
      var a = ['[xtq-popup]'];
      for (var i = 0; i < arguments.length; i++) a.push(arguments[i]);
      try { global.console.log.apply(global.console, a); } catch (e) {}
    }
  }
  function warn() {
    if (global.console && global.console.warn) {
      var a = ['[xtq-popup]'];
      for (var i = 0; i < arguments.length; i++) a.push(arguments[i]);
      try { global.console.warn.apply(global.console, a); } catch (e) {}
    }
  }
  // 强可见的提示：先 console.warn 再 alert（最后兜底，确保用户能看到）
  function alertMsg(text) {
    warn(text);
    try { global.alert(text); } catch (e) {}
  }

  // 暴露给模板调试用：window.xtqDebug = true 后所有日志都打
  global.xtqDebug = false;
  function dlog() {
    if (global.xtqDebug) log.apply(null, arguments);
  }

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
  // 浏览器处理失败时会立即输出 "Failed to launch ... because the scheme does not have a registered handler"
  // 这种错误在主 console 上；我们用一个 error listener 捕获，立即 resolve(false) 走降级。
  function tryProtocol(protocolUrl) {
    log('tryProtocol', protocolUrl);
    return new Promise(function (resolve) {
      var fired = false;
      var anchor = document.createElement('a');
      anchor.href = protocolUrl;
      anchor.style.display = 'none';
      anchor.rel = 'noopener';
      anchor.target = '_self';
      document.body.appendChild(anchor);

      function done(invoked, why) {
        if (fired) return;
        fired = true;
        cleanup();
        log('tryProtocol result: invoked=' + invoked + (why ? ' (' + why + ')' : ''));
        resolve(invoked);
      }

      function onBlur() {
        done(true, 'window blur');
      }
      function onVis() {
        if (document.hidden) done(true, 'document.hidden');
      }

      // 监听浏览器对协议处理的错误输出：「Failed to launch '...' because the scheme does not have a registered handler」
      // 这类消息会作为 error 事件冒泡到 window（Chrome/Edge 行为）。捕获到即代表未注册。
      function onErr(e) {
        var msg = e && (e.message || '');
        if (/scheme does not have a registered handler|user gesture is required|Not allowed to launch/i.test(msg)) {
          done(false, 'browser rejected: ' + msg);
        }
      }

      function cleanup() {
        window.removeEventListener('blur', onBlur, true);
        document.removeEventListener('visibilitychange', onVis);
        window.removeEventListener('error', onErr, true);
        if (anchor && anchor.parentNode) anchor.parentNode.removeChild(anchor);
        if (timer) clearTimeout(timer);
      }

      window.addEventListener('blur', onBlur, true);
      document.addEventListener('visibilitychange', onVis);
      window.addEventListener('error', onErr, true);

      // 兜底超时：800ms 内没失焦也没收到错误 → 视为未注册
      var timer = setTimeout(function () { done(false, 'timeout ' + PROTOCOL_TIMEOUT_MS + 'ms'); }, PROTOCOL_TIMEOUT_MS);

      // 用 MouseEvent 让 Chrome/Edge 视为「用户手势」允许协议唤起
      try {
        var ev = new MouseEvent('click', { bubbles: true, cancelable: true, view: window, button: 0 });
        anchor.dispatchEvent(ev);
        log('dispatched MouseEvent click on protocol anchor');
      } catch (e) {
        warn('dispatchEvent failed, fallback to anchor.click()', e);
        try { anchor.click(); } catch (e2) { warn('anchor.click failed', e2); }
      }
    });
  }

  // 备用：直接用 location.href 触发协议（必须在用户手势同步上下文里调用，否则浏览器会拒绝）
  // 所以这里不再等待 setTimeout，而是立即执行（如果第一次 anchor.click 在同步上下文失败，
  // 那 location.href 在异步上下文里也会失败 —— 浏览器已经失去 user gesture）
  // 因此这里直接返回 false，交给外层做 window.open 降级。
  function tryProtocolViaLocation(protocolUrl) {
    log('tryProtocolViaLocation skipped: user gesture would be lost; rely on window.open fallback');
    return Promise.resolve(false);
  }

  // 显示消息（优先用 Element-UI 的 $message，回退到 alert）
  function showMsg(win, type, text) {
    try {
      if (win && win.Vue && win.Vue.prototype && win.Vue.prototype.$message) {
        win.Vue.prototype.$message[type](text);
        return;
      }
    } catch (e) {}
    if (type === 'error') {
      alertMsg(text);
    } else {
      log(type + ':', text);
    }
  }

  /**
   * 弹出独立窗口。
   */
  function launchPopup(opts) {
    log('launchPopup() called with', opts);
    opts = opts || {};
    var postUrl = opts.url;
    var body = opts.body || {};
    var title = opts.title || '独立窗口';
    var vm = opts.vm || null;
    var onStart = opts.onStart || function () {};
    var onEnd = opts.onEnd || function () {};

    if (!postUrl) {
      alertMsg('launchPopup: 缺少 url 参数');
      onEnd();
      return Promise.resolve(false);
    }

    onStart();

    return Promise.resolve()
      .then(function () {
        log('POST', postUrl, 'body=', body);
        return fetch(postUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
          cache: 'no-store',
          credentials: 'same-origin',
          keepalive: true,  // 关键：B 端页面切走时请求不被取消
        });
      })
      .then(function (r) {
        log('fetch response status', r.status);
        if (!r.ok) {
          throw new Error('HTTP ' + r.status + ' ' + r.statusText);
        }
        return r.json();
      })
      .then(function (resp) {
        log('fetch response body', resp);
        if (!resp || !resp.ok) {
          var err = (resp && resp.error) || '启动失败';
          showMsg(vm ? (vm.$root || vm) : window, 'error', title + '启动失败：' + err);
          return false;
        }
        if (resp.action === 'local_started') {
          // A 本机：后端已 Popen，弹窗在服务器桌面弹出
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
          log('client_launch action, trying protocol', protoUrl);
          return tryProtocol(protoUrl).then(function (invoked) {
            if (invoked) {
              showMsg(vm ? (vm.$root || vm) : window, 'success', title + '已唤起本机弹窗');
              return true;
            }
            // 协议未注册/唤起失败，最终降级：浏览器新标签打开弹窗 URL
            // 同时明确告诉用户：想让 PyWebView 弹窗就执行 popup_launcher.exe --register 注册协议
            log('final fallback: window.open', resp.url);
            try {
              var win = global.open(resp.url, '_blank', 'noopener');
              if (!win) {
                alertMsg('浏览器拦截了新窗口。请在地址栏左侧允许弹窗，或手动访问：' + resp.url);
              } else {
                showMsg(vm ? (vm.$root || vm) : window, 'success', title + '已在新标签打开（PyWebView 未注册协议，浏览器降级）');
              }
            } catch (e) {
              alertMsg(title + '无法打开窗口：' + e.message + '\n请手动访问：' + resp.url);
              return false;
            }
            return true;
          });
        }
        showMsg(vm ? (vm.$root || vm) : window, 'error', title + '：未知 action=' + resp.action);
        return false;
      })
      .catch(function (err) {
        warn('launchPopup error', err);
        alertMsg(title + '启动失败：' + (err && err.message ? err.message : err) +
                 '\n（请打开 F12 控制台查看 [xtq-popup] 日志）');
        return false;
      })
      .then(function (ok) { onEnd(); return ok; });
  }

  // 暴露 API
  global.xtquantLaunchPopup = launchPopup;
  log('popup-launcher.js loaded, xtquantLaunchPopup is ready');
  // ---- 全局错误捕获：把任何未处理的 JS 错误 alert 出来（最后兜底）----
  // 这样即使按钮方法里 try/catch 漏了或 fetch 异常路径没走通，用户也能看到错误。
  global.addEventListener('error', function (e) {
    var msg = '[xtq-popup] Uncaught error: ' + (e && e.message) +
              (e && e.filename ? ' at ' + e.filename + ':' + e.lineno : '');
    warn(msg, e && e.error);
    // 不 alert 所有 error（会刷屏），只 alert 包含 xtquantLaunchPopup 关键词的
    if (e && e.error && /xtquantLaunchPopup|launchPopup/.test(String(e.error.stack || e.error))) {
      try { global.alert(msg); } catch (ex) {}
    }
  });
  global.addEventListener('unhandledrejection', function (e) {
    var r = e && e.reason;
    var msg = '[xtq-popup] Unhandled promise rejection: ' + (r && r.message ? r.message : r);
    warn(msg, r);
    try { global.alert(msg + '\n（F12 Console 看 [xtq-popup] 详细日志）'); } catch (ex) {}
  });
})(typeof window !== 'undefined' ? window : this);
