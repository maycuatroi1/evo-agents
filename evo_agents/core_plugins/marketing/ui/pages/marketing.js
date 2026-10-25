// What the pages of the core plugin marketing share on top of common.js: the tabs between its three framed pages, a
// reader's note, the month's cost against its cap as a meter, and the money the month has used.
(function () {
  "use strict";
  var evo = window.evo;
  var page = window.page;
  var el = page.el;
  var t = page.t;

  var TABS = ["overview", "posts", "weekly"];

  /** The plugin's three pages as tabs under the page head: links that open the other framed page in the hub. */
  function tabs(current) {
    return el(
      "nav",
      { class: "evo-tabs", "aria-label": t("tabs.label") },
      TABS.map(function (id) {
        return page.link(page.pagePath("marketing", id), { class: "evo-tab", "aria-current": id === current ? "page" : null, testid: "marketing-tab-" + id }, t("tabs." + id));
      }),
    );
  }

  /** A reader's note: they read the channels, the budget and the week; nothing on the page writes for them. */
  function readerNote(text) {
    return page.banner(t("readerTitle", { project: evo.context.project }), text || t("readerText"), { testid: "marketing-read-only" });
  }

  /** What the month has used of its cap: spent plus what posts in flight hold, 0 to 100 (more once over). */
  function budgetUsed(budget) {
    if (budget.budget_usd <= 0) return 100;
    return ((budget.spent_usd + budget.reserved_usd) / budget.budget_usd) * 100;
  }

  /** The month's money is gone: nothing more is posted that costs until the next month or a higher cap. */
  function budgetSpent(budget) {
    return budget.spent_usd + budget.reserved_usd >= budget.budget_usd;
  }

  var meters = 0;

  /** The month's cost against its cap: a meter, and the figures written beside it so the colour is never the only cue. */
  function budgetMeter(budget) {
    meters += 1;
    var id = "marketing-budget-label-" + meters;
    var used = budgetUsed(budget);
    var tone = used >= 100 ? "danger" : used >= 80 ? "warning" : "normal";
    var shown = Math.round(Math.min(Math.max(used, 0), 100));
    var figures = t("budget.figures", { used: page.money(budget.spent_usd + budget.reserved_usd), cap: page.money(budget.budget_usd) });
    return el("div", { class: "evo-stack-sm", testid: "marketing-budget" }, [
      el("div", { class: "evo-row evo-row-between" }, [
        el("span", { id: id, class: "evo-small evo-muted", text: t("budget.label", { month: budget.month }) }),
        el("span", { class: "page-figure page-tone-" + tone, testid: "marketing-budget-figures", text: figures }),
      ]),
      el(
        "div",
        { class: "page-meter page-meter-" + tone, role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(shown), "aria-valuetext": figures, "aria-labelledby": id },
        el("span", { style: "width: " + shown + "%" }),
      ),
      el("p", { class: "evo-small evo-muted" }, [
        t("budget.split", { spent: page.money(budget.spent_usd), reserved: page.money(budget.reserved_usd) }),
        used >= 100 ? el("span", { class: "page-strong page-danger", text: " " + t("budget.spentAll") }) : null,
      ]),
    ]);
  }

  page.marketing = { tabs: tabs, readerNote: readerNote, budgetMeter: budgetMeter, budgetSpent: budgetSpent };
})();
