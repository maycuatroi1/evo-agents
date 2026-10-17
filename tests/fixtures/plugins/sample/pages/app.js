// The page of the sample plugin: a client of the hub's bridge evo-plugin/1 (docs/plugins.md, Pages), and the checks
// web/e2e/plugins.spec.ts reads from the frame. The frame is sandboxed with an opaque origin: reading the hub's
// cookies or the page around it throws, and the CSP refuses a fetch of its own.
(function () {
  "use strict";

  var BRIDGE = "evo-plugin/1";
  var pending = new Map();
  var next = 1;

  function call(method, params) {
    return new Promise(function (resolve, reject) {
      var id = "sample-" + next++;
      pending.set(id, { resolve: resolve, reject: reject });
      window.parent.postMessage({ bridge: BRIDGE, id: id, method: method, params: params || {} }, "*");
    });
  }

  window.addEventListener("message", function (event) {
    if (event.source !== window.parent) return;
    var data = event.data;
    if (!data || data.bridge !== BRIDGE) return;
    if (data.event === "theme") {
      document.documentElement.dataset.theme = data.theme;
      return;
    }
    var waiting = pending.get(data.id);
    if (!waiting) return;
    pending.delete(data.id);
    if (data.error) waiting.reject(data.error);
    else waiting.resolve(data.result);
  });

  function show(id, text) {
    document.getElementById(id).textContent = text;
  }

  function resize() {
    return call("resize", { height: document.documentElement.scrollHeight });
  }

  try {
    show("cookie", document.cookie ? "readable" : "readable, empty");
  } catch (error) {
    show("cookie", "blocked: " + error.name);
  }
  try {
    show("parent", "readable: " + window.parent.document.title);
  } catch (error) {
    show("parent", "blocked: " + error.name);
  }
  fetch("/v1/auth/web/csrf").then(
    function () {
      show("fetch", "allowed");
    },
    function (error) {
      show("fetch", "blocked: " + error.name);
    },
  );

  call("context")
    .then(function (context) {
      document.documentElement.dataset.theme = context.theme;
      show("context", context.plugin + " in " + context.project + " for " + context.viewer.login);
      show("methods", context.methods.join(", "));
      return call("runs.list", { limit: 5 });
    })
    .then(
      function (list) {
        show("runs", "allowed: " + list.total + " runs");
      },
      function (error) {
        show("runs", "refused: " + error.code);
      },
    )
    .then(function () {
      return call("plans.list");
    })
    .then(
      function () {
        show("plans", "allowed");
      },
      function (error) {
        show("plans", "refused: " + error.code);
      },
    )
    .then(resize, resize);
})();
