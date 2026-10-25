/*
 * evo-ui.js, the UI kit 1 of the pages of isolated plugins (docs/plugins.md, Pages). Hand-written ES2019, no build;
 * served by the hub at /v1/plugin-ui/kit/1/evo-ui.js beside evo-ui.css. A page framed by the hub's web loads both:
 *
 *   <link rel="stylesheet" href="/v1/plugin-ui/kit/1/evo-ui.css">
 *   <script src="/v1/plugin-ui/kit/1/evo-ui.js"></script>
 *
 * and gets window.evo: the bridge evo-plugin/1 as promises, the viewer's theme on <html> (class "dark"), the locale
 * as <html lang>, its own words from i18n/<locale>.js beside the page (each calls evo.messages(locale, {...})), and
 * the frame's height kept to the page's own (<html data-evo-resize="off"> turns that off).
 */
(function () {
  "use strict";

  var BRIDGE = "evo-plugin/1";
  var LOCALES = ["en", "vi"];
  var root = document.documentElement;
  var framed = window.parent !== window;
  var pending = {};
  var next = 0;
  var dictionaries = {};
  var listeners = { theme: [], location: [] };
  var locale = "en";

  function bridgeError(error) {
    var failure = new Error((error && error.message) || "the hub did not answer");
    failure.code = (error && error.code) || "hub_unavailable";
    if (error && typeof error.status === "number") failure.status = error.status;
    return failure;
  }

  /** Calls `method` of the bridge; resolves with its result, rejects with an Error that has code (and status). */
  function call(method, params) {
    if (!framed) return Promise.reject(bridgeError({ code: "not_framed", message: "this page is not framed by the hub" }));
    next += 1;
    var id = "evo-" + next;
    return new Promise(function (resolve, reject) {
      pending[id] = { resolve: resolve, reject: reject };
      window.parent.postMessage({ bridge: BRIDGE, id: id, method: method, params: params || {} }, "*");
    });
  }

  function emit(name, value) {
    listeners[name].slice().forEach(function (listener) {
      try {
        listener(value);
      } catch (error) {
        setTimeout(function () {
          throw error;
        });
      }
    });
  }

  function applyTheme(theme) {
    root.classList.toggle("dark", theme === "dark");
  }

  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!framed || event.source !== window.parent || !data || data.bridge !== BRIDGE) return;
    if (data.event === "theme" && (data.theme === "light" || data.theme === "dark")) {
      if (evo.context) evo.context.theme = data.theme;
      applyTheme(data.theme);
      emit("theme", data.theme);
      return;
    }
    if (data.event === "location" && data.location && typeof data.location === "object") {
      if (evo.context) evo.context.location = data.location;
      emit("location", data.location);
      return;
    }
    var waiting = Object.prototype.hasOwnProperty.call(pending, data.id) ? pending[data.id] : null;
    if (!waiting) return;
    delete pending[data.id];
    if (data.error) waiting.reject(bridgeError(data.error));
    else waiting.resolve(data.result);
  });

  function lookup(dictionary, key) {
    if (!dictionary) return undefined;
    if (typeof dictionary[key] === "string") return dictionary[key];
    var value = key.split(".").reduce(function (node, part) {
      return node && typeof node === "object" ? node[part] : undefined;
    }, dictionary);
    return typeof value === "string" ? value : undefined;
  }

  /** The words of `key` in the viewer's locale, else in English, else the key itself; {name} takes vars.name. */
  function t(key, vars) {
    var text = lookup(dictionaries[locale], key);
    if (text === undefined) text = lookup(dictionaries.en, key);
    if (text === undefined) text = key;
    return text.replace(/\{(\w+)\}/g, function (whole, name) {
      return vars && Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : whole;
    });
  }

  /** Adds the words of `dict` to those of `lang`, as i18n/<lang>.js does: evo.messages("vi", {...}). */
  function messages(lang, dict) {
    var into = dictionaries[lang] || (dictionaries[lang] = {});
    Object.keys(dict || {}).forEach(function (key) {
      into[key] = dict[key];
    });
  }

  /** Loads i18n/<lang>.js beside the page; a page without one is fine, t then answers in English or with the key. */
  function loadMessages(lang) {
    return new Promise(function (resolve) {
      var script = document.createElement("script");
      script.src = "i18n/" + lang + ".js";
      script.onload = resolve;
      script.onerror = resolve;
      (document.head || root).appendChild(script);
    });
  }

  function localeOf(value) {
    var base = String(value || "").toLowerCase().split("-")[0];
    return LOCALES.indexOf(base) >= 0 ? base : "en";
  }

  /** The hub path of this page, with `sub` after it: /p/<project>/x/<plugin>/<page>/<sub>. */
  function pageHref(sub) {
    var context = evo.context;
    var base = "/p/" + [context.project, "x", context.plugin, context.page].map(encodeURIComponent).join("/");
    return sub ? base + "/" + sub.replace(/^\/+/, "") : base;
  }

  /**
   * Opens `to` in the hub: a path that starts with "/" as it is (only one under /p/<this project>/ is allowed), one
   * that starts with "?" as this page and sub path with that query, anything else as a sub path of this page.
   */
  function navigate(to) {
    return evo.ready.then(function (context) {
      var target = String(to);
      if (target.charAt(0) === "?") target = pageHref(context.location.path) + target;
      else if (target.charAt(0) !== "/") target = pageHref(target);
      return call("navigate", { to: target });
    });
  }

  function toast(text, tone) {
    return call("toast", { text: text, tone: tone || "info" });
  }

  /** Calls the plugin's own route `path`: options method (GET), query and body; resolves with {status, body}. */
  function route(path, options) {
    var params = { path: path };
    options = options || {};
    if (options.method) params.method = options.method;
    if (options.query) params.query = options.query;
    if (options.body !== undefined) params.body = options.body;
    return call("route", params);
  }

  var lastHeight = 0;
  var sizing = false;
  /** Sizes the frame to `height`, or to the page's own height when left out. */
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

  /** Calls `listener` on each "theme" ("light" or "dark") or "location" ({path, query}); returns how to stop. */
  function on(name, listener) {
    if (!listeners[name]) throw new Error("evo.on knows theme and location, not " + name);
    listeners[name].push(listener);
    return function () {
      listeners[name] = listeners[name].filter(function (each) {
        return each !== listener;
      });
    };
  }

  var evo = {
    bridge: BRIDGE,
    kit: 1,
    context: null,
    ready: null,
    call: call,
    route: route,
    navigate: navigate,
    toast: toast,
    resize: resize,
    on: on,
    t: t,
    messages: messages,
    href: function (sub) {
      return pageHref(sub || "");
    },
  };
  window.evo = evo;

  evo.ready = call("context").then(function (context) {
    evo.context = context;
    locale = localeOf(context.locale);
    applyTheme(context.theme);
    root.setAttribute("lang", locale);
    var loads = [loadMessages("en")];
    if (locale !== "en") loads.push(loadMessages(locale));
    return Promise.all(loads).then(function () {
      if (document.body) followHeight();
      else document.addEventListener("DOMContentLoaded", followHeight);
      return context;
    });
  });
})();
