// What the pages of this plugin share, on top of the hub's UI kit 1 (window.evo): words with ICU plurals, elements,
// dates and money in the viewer's locale, the plugin's routes as promises that reject with the hub's refusal, links
// that open in the hub's page around the frame, a confirm dialog and a banner. Plain ES2017, no build.
(function () {
  "use strict";
  var evo = window.evo;

  function locale() {
    return (evo.context && evo.context.locale) || document.documentElement.lang || "en";
  }

  function pluralForm(count) {
    try {
      return new Intl.PluralRules(locale()).select(count);
    } catch (error) {
      return count === 1 ? "one" : "other";
    }
  }

  // The end of the block that opens at `start` ("{"), or -1.
  function closing(text, start) {
    var depth = 0;
    for (var index = start; index < text.length; index += 1) {
      if (text[index] === "{") depth += 1;
      else if (text[index] === "}") {
        depth -= 1;
        if (depth === 0) return index;
      }
    }
    return -1;
  }

  // `{name, plural, =0 {...} one {...} other {...}}` in `text`, with # the number, as the hub's web words have them.
  function plurals(text, vars) {
    var opening = /\{(\w+), plural,/g;
    var out = "";
    var from = 0;
    var found;
    while ((found = opening.exec(text)) !== null) {
      var end = closing(text, found.index);
      if (end < 0) break;
      var count = Number(vars && vars[found[1]]);
      var body = text.slice(found.index + found[0].length, end);
      var options = {};
      var choice = /\s*(=\d+|zero|one|two|few|many|other)\s*\{/g;
      var option;
      while ((option = choice.exec(body)) !== null) {
        var stop = closing(body, choice.lastIndex - 1);
        if (stop < 0) break;
        options[option[1]] = body.slice(choice.lastIndex, stop);
        choice.lastIndex = stop + 1;
      }
      var picked = options["=" + count];
      if (picked === undefined) picked = options[pluralForm(count)];
      if (picked === undefined) picked = options.other || "";
      out += text.slice(from, found.index) + picked.replace(/#/g, String(count));
      from = end + 1;
      opening.lastIndex = from;
    }
    return out + text.slice(from);
  }

  /** The words of `key` with `vars`, plurals resolved. */
  function t(key, vars) {
    return plurals(evo.t(key, vars), vars);
  }

  /**
   * An element: `props` sets its class (`class`), text (`text`), data-testid (`testid`), listeners (`on`) and any
   * other attribute (false or null leaves it out); `children` are nodes, strings or arrays of them, null skipped.
   */
  function el(tag, props, children) {
    var node = document.createElement(tag);
    Object.keys(props || {}).forEach(function (name) {
      var value = props[name];
      if (value === null || value === undefined || value === false) return;
      if (name === "class") node.className = value;
      else if (name === "text") node.textContent = value;
      else if (name === "testid") node.setAttribute("data-testid", value);
      else if (name === "on") {
        Object.keys(value).forEach(function (event) {
          node.addEventListener(event, value[event]);
        });
      } else node.setAttribute(name, value === true ? "" : String(value));
    });
    append(node, children);
    return node;
  }

  function append(node, children) {
    if (children === null || children === undefined || children === false) return;
    if (Array.isArray(children)) {
      children.forEach(function (child) {
        append(node, child);
      });
      return;
    }
    node.appendChild(typeof children === "string" || typeof children === "number" ? document.createTextNode(String(children)) : children);
  }

  /** Empties `node` and fills it with `children`. */
  function fill(node, children) {
    while (node.firstChild) node.removeChild(node.firstChild);
    append(node, children);
    return node;
  }

  /** A time, short for dense lists, with the full time as its tooltip. */
  function when(value) {
    if (!value) return el("span", { class: "evo-muted" });
    var date = new Date(value);
    var full = new Intl.DateTimeFormat(locale(), { dateStyle: "medium", timeStyle: "short" }).format(date);
    var short = new Intl.DateTimeFormat(locale(), { dateStyle: "short", timeStyle: "short" }).format(date);
    return el("time", { datetime: value, title: full, class: "page-when", text: short });
  }

  /** A day, as dateStyle medium. */
  function day(value) {
    return new Intl.DateTimeFormat(locale(), { dateStyle: "medium" }).format(new Date(value));
  }

  /** Dollars as the hub counts them, to the cent. */
  function money(usd) {
    return new Intl.NumberFormat(locale(), { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(usd);
  }

  function number(value, options) {
    return new Intl.NumberFormat(locale(), options).format(value);
  }

  /** The hub's refusal of a route as an Error: its message, and the status. */
  function refusal(answer) {
    var body = answer && answer.body;
    var message = body && typeof body.message === "string" ? body.message : "the hub answered " + answer.status;
    var error = new Error(message);
    error.status = answer.status;
    return error;
  }

  /** Calls the plugin's route `path` (evo.route); resolves with the body of a 2xx answer, rejects with the refusal. */
  function call(path, options) {
    return evo.route(path, options).then(function (answer) {
      if (answer.status >= 400) throw refusal(answer);
      return answer.body;
    });
  }

  /** What a failed write says: the words `byStatus` gives its status, else the hub's own message. */
  function failure(error, byStatus) {
    if (error && byStatus && byStatus[error.status]) return byStatus[error.status];
    return (error && error.message) || "the hub did not answer";
  }

  /** The viewer's role on the project: reader, writer or admin, null for none. */
  function role() {
    var value = evo.context && evo.context.viewer ? evo.context.viewer.role : null;
    return value === "reader" || value === "writer" || value === "admin" ? value : null;
  }

  function canWrite() {
    return role() === "writer" || role() === "admin";
  }

  /** The hub's path of page `page` of plugin `plugin` in this project, with `sub` after it. */
  function pagePath(plugin, page, sub) {
    var base = "/p/" + [evo.context.project, "x", plugin, page].map(encodeURIComponent).join("/");
    return sub ? base + "/" + sub : base;
  }

  /** A link to `to`, a path of the hub's web, opened in the hub's page around the frame. */
  function link(to, props, children) {
    var options = Object.assign({ href: to }, props || {});
    var node = el("a", options, children);
    node.addEventListener("click", function (event) {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      evo.navigate(to).catch(function (error) {
        evo.toast(failure(error), "error");
      });
    });
    return node;
  }

  /** A link out of the hub, to a page the frame's sandbox opens in a tab of its own where the browser lets it. */
  function external(href, props, children) {
    return el("a", Object.assign({ href: href, target: "_blank", rel: "noreferrer" }, props || {}), children);
  }

  function badge(text, variant, props) {
    return el("span", Object.assign({ class: "evo-badge" + (variant ? " evo-badge-" + variant : ""), text: text }, props || {}));
  }

  /** A banner across the page: a title and a line under it; tone danger for what stopped. */
  function banner(title, text, props, tone) {
    return el("div", Object.assign({ role: "note", class: "page-banner" + (tone ? " page-banner-" + tone : "") }, props || {}), [
      el("p", { class: "page-banner-title", text: title }),
      text ? el("p", { class: "page-banner-text", text: text }) : null,
    ]);
  }

  /** A card that is a region named by its h2: `head` beside the title (a count), then `body`. */
  function section(id, title, head, body, props) {
    var titleId = id + "-title";
    return el("section", Object.assign({ class: "evo-card", role: "region", "aria-labelledby": titleId, testid: id }, props || {}), [
      el("div", { class: "page-section-head" }, [el("h2", { id: titleId, class: "evo-card-title", text: title }), head || null]),
      body,
    ]);
  }

  /**
   * Asks before a write: a dialog (role alertdialog) with `title`, `text`, the error of a failed try, Cancel and the
   * confirm button (data-testid `<testId>-confirm`); `run` returns a promise, and the dialog closes once it resolves.
   */
  function confirm(options) {
    var error = el("p", { class: "evo-error", role: "alert", hidden: true });
    var cancel = el("button", { type: "button", class: "evo-btn", text: options.cancel });
    var go = el("button", {
      type: "button",
      class: "evo-btn " + (options.danger ? "evo-btn-destructive" : "evo-btn-primary"),
      text: options.confirm,
      testid: options.testId + "-confirm",
    });
    var titleId = options.testId + "-title";
    var textId = options.testId + "-text";
    var dialog = el("dialog", { class: "page-dialog", role: "alertdialog", "aria-modal": "true", "aria-labelledby": titleId, "aria-describedby": textId, testid: options.testId }, [
      el("h2", { id: titleId, class: "evo-card-title", text: options.title }),
      el("p", { id: textId, class: "evo-muted", text: options.text }),
      error,
      el("div", { class: "evo-row evo-row-end" }, [cancel, go]),
    ]);
    var busy = false;
    function close() {
      dialog.close();
      dialog.remove();
    }
    cancel.addEventListener("click", function () {
      if (!busy) close();
    });
    dialog.addEventListener("cancel", function (event) {
      if (busy) event.preventDefault();
    });
    dialog.addEventListener("close", function () {
      if (dialog.isConnected) dialog.remove();
    });
    go.addEventListener("click", function () {
      busy = true;
      go.disabled = true;
      cancel.disabled = true;
      go.textContent = options.pending;
      go.setAttribute("aria-busy", "true");
      error.hidden = true;
      Promise.resolve()
        .then(options.run)
        .then(
          function () {
            busy = false;
            close();
          },
          function (cause) {
            busy = false;
            go.disabled = false;
            cancel.disabled = false;
            go.textContent = options.confirm;
            go.removeAttribute("aria-busy");
            error.textContent = failure(cause, options.byStatus);
            error.hidden = false;
          },
        );
    });
    document.body.appendChild(dialog);
    dialog.showModal();
    cancel.focus();
    return dialog;
  }

  /** The words of every [data-i18n] element. */
  function words(root) {
    (root || document).querySelectorAll("[data-i18n]").forEach(function (node) {
      node.textContent = t(node.getAttribute("data-i18n"));
    });
  }

  /** What the page shows while its read failed: the hub's message and Try again. */
  function failed(error, retry) {
    return el("div", { class: "evo-empty", role: "alert", testid: "page-error" }, [
      el("p", { class: "evo-empty-title", text: failure(error) }),
      retry ? el("button", { type: "button", class: "evo-btn", text: t("common.retry"), on: { click: retry } }) : null,
    ]);
  }

  window.page = {
    t: t,
    el: el,
    fill: fill,
    when: when,
    day: day,
    money: money,
    number: number,
    call: call,
    failure: failure,
    role: role,
    canWrite: canWrite,
    pagePath: pagePath,
    link: link,
    external: external,
    badge: badge,
    banner: banner,
    section: section,
    confirm: confirm,
    words: words,
    failed: failed,
  };
})();
