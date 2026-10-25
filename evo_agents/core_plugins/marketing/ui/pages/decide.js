// The panel of the core plugin marketing on a project's Decisions page (slot "decide"): what waits for a person in the
// project's growth, each channel whose breaker is open (only an admin closes it), each browser channel whose login
// session expired (its owner logs in again), and the month's budget once it is spent. While nothing waits it shows
// nothing and sizes its frame to 0, so the panel takes no room; it sizes the frame itself, to its content.
(function () {
  "use strict";
  var evo = window.evo;
  var page = window.page;
  var marketing = page.marketing;
  var el = page.el;
  var t = page.t;

  var root = document.getElementById("page");
  var MARK = { breaker: "⏻", session: "⚿", budget: "$" };

  /** The items that wait, breakers first, then expired sessions, then the budget. */
  function growthItems(channels, status) {
    var items = [];
    channels.forEach(function (channel) {
      if (channel.paused) items.push({ kind: "breaker", title: t("decide.breaker", { channel: channel.channel, account: channel.account }) });
    });
    channels.forEach(function (channel) {
      if (channel.adapter === "browser" && channel.session_state === "stale") items.push({ kind: "session", title: t("decide.session", { channel: channel.channel, account: channel.account }) });
    });
    if (marketing.budgetSpent(status.budget)) items.push({ kind: "budget", title: t("decide.budget", { month: status.budget.month, cap: page.money(status.budget.budget_usd) }) });
    return items;
  }

  function item(entry) {
    return el("li", { class: "decide-item", testid: "growth-item", "data-kind": entry.kind }, [
      el("span", { class: "decide-mark", "aria-hidden": "true", text: MARK[entry.kind] }),
      el("div", { class: "evo-grow evo-stack-sm decide-text" }, [
        el("span", { class: "evo-badge evo-badge-outline decide-tag", text: t("decide.kind." + entry.kind) }),
        el("p", { class: "page-wrap", text: entry.title }),
        el("p", { class: "evo-small evo-subtle", text: t("decide.who." + entry.kind) }),
      ]),
      page.link(page.pagePath("marketing", "overview"), { class: "evo-btn evo-btn-secondary evo-btn-sm", "aria-label": t("decide.openLabel", { title: entry.title }), testid: "growth-item-open" }, "↗ " + t("decide.open")),
    ]);
  }

  function panel(items) {
    return el("section", { class: "decide-panel", "aria-labelledby": "decide-growth-title", testid: "decide-growth" }, [
      el("div", { class: "decide-head" }, [
        el("h2", { id: "decide-growth-title", class: "evo-card-title", text: t("decide.title") }),
        el("span", { class: "evo-small evo-subtle", text: t("decide.note") }),
        el("span", { class: "decide-count" }, [el("span", { "aria-hidden": "true", text: String(items.length) }), el("span", { class: "evo-sr-only", text: t("decide.waiting", { count: items.length }) })]),
      ]),
      el("ul", { class: "page-list decide-list" }, items.map(item)),
    ]);
  }

  /** Sizes the frame to the panel, or to 0 while it shows nothing. */
  function fit() {
    var height = root.firstChild ? Math.ceil(root.getBoundingClientRect().height) : 0;
    evo.resize(height).catch(function () {});
  }

  function show() {
    return Promise.all([page.call("channels"), page.call("status")]).then(
      function (answers) {
        var items = growthItems(answers[0], answers[1]);
        page.fill(root, items.length ? panel(items) : null);
        fit();
      },
      function () {
        page.fill(root, null);
        fit();
      },
    );
  }

  evo.ready.then(function () {
    evo.resize(0).catch(function () {});
    show();
    if (typeof ResizeObserver === "function") new ResizeObserver(fit).observe(root);
  });
})();
