/*
 * evo-answer.js, the helper of interactive answers in the hub's chats (docs/hub.md, Interactive answers). Hand-written
 * ES2019, no build; served by the hub at /v1/plugin-ui/kit/1/evo-answer.js beside evo-ui.css. An answer an agent
 * writes loads both, in a chat that is not restricted:
 *
 *   <link rel="stylesheet" href="/v1/plugin-ui/kit/1/evo-ui.css">
 *   <script src="/v1/plugin-ui/kit/1/evo-answer.js"></script>
 *
 * and gets window.evoAnswer: the bridge evo-answer/1 as promises (context, resize, sendPrompt, openLink), the viewer's
 * theme on <html> (class "dark", which evo-ui.css reads), the language as <html lang>, and the frame's height kept to
 * the page's own (<html data-evo-resize="off"> turns that off). A restricted chat serves the answer without 'self',
 * so this file does not load there: a page that must work there too posts the same messages itself.
 */
(function () {
  "use strict";

  var BRIDGE = "evo-answer/1";
  var root = document.documentElement;
  var framed = window.parent !== window;
  var pending = {};
  var next = 0;
  var themeListeners = [];

  function bridgeError(error) {
    var failure = new Error((error && error.message) || "the hub did not answer");
    failure.code = (error && error.code) || "hub_unavailable";
    if (error && typeof error.status === "number") failure.status = error.status;
    return failure;
  }

  /** Calls `method` of the bridge; resolves with its result, rejects with an Error that has code (and status). */
  function call(method, params) {
    if (!framed) return Promise.reject(bridgeError({ code: "not_framed", message: "this answer is not framed by the hub" }));
    next += 1;
    var id = "answer-" + next;
    return new Promise(function (resolve, reject) {
      pending[id] = { resolve: resolve, reject: reject };
      window.parent.postMessage({ bridge: BRIDGE, id: id, method: method, params: params || {} }, "*");
    });
  }

  function applyTheme(theme) {
    root.classList.toggle("dark", theme === "dark");
  }

  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!framed || event.source !== window.parent || !data || data.bridge !== BRIDGE) return;
    if (data.event === "theme" && (data.theme === "light" || data.theme === "dark")) {
      if (evoAnswer.context) evoAnswer.context.theme = data.theme;
      applyTheme(data.theme);
      themeListeners.slice().forEach(function (listener) {
        try {
          listener(data.theme);
        } catch (error) {
          setTimeout(function () {
            throw error;
          });
        }
      });
      return;
    }
    var waiting = Object.prototype.hasOwnProperty.call(pending, data.id) ? pending[data.id] : null;
    if (!waiting) return;
    delete pending[data.id];
    if (data.error) waiting.reject(bridgeError(data.error));
    else waiting.resolve(data.result);
  });

  var lastHeight = 0;
  var sizing = false;
  /** Sizes the frame to `height`, or to the page's own height when left out; the web keeps it within bounds. */
  function resize(height) {
    var wanted = typeof height === "number" ? height : Math.ceil(root.scrollHeight);
    if (wanted === lastHeight) return Promise.resolve({ height: wanted });
    lastHeight = wanted;
    return call("resize", { height: wanted });
  }

  function followHeight() {
    if (root.getAttribute("data-evo-resize") === "off" || typeof ResizeObserver !== "function") return;
    var observer = new ResizeObserver(function () {
      if (sizing) return;
      sizing = true;
      requestAnimationFrame(function () {
        sizing = false;
        resize().catch(function () {});
      });
    });
    observer.observe(document.body || root);
  }

  var evoAnswer = {
    bridge: BRIDGE,
    context: null,
    ready: null,
    call: call,
    resize: resize,
    /** Sends `text` (1 to 4000 characters) as the viewer's next question to the agent that wrote this answer. */
    sendPrompt: function (text) {
      return call("sendPrompt", { text: String(text) });
    },
    /** Opens `url`, an https URL, in a new tab of the viewer's browser. */
    openLink: function (url) {
      return call("openLink", { url: String(url) });
    },
    /** Calls `listener` with "light" or "dark" each time the viewer's theme changes; returns how to stop. */
    onTheme: function (listener) {
      themeListeners.push(listener);
      return function () {
        themeListeners = themeListeners.filter(function (each) {
          return each !== listener;
        });
      };
    },
  };
  window.evoAnswer = evoAnswer;

  evoAnswer.ready = call("context").then(function (context) {
    evoAnswer.context = context;
    applyTheme(context.theme);
    root.setAttribute("lang", String(context.locale || "en").split("-")[0]);
    if (document.body) followHeight();
    else document.addEventListener("DOMContentLoaded", followHeight);
    return context;
  });
  // A page that never awaits ready still sizes itself; an error stays on the promise.
  evoAnswer.ready.catch(function () {});
})();
