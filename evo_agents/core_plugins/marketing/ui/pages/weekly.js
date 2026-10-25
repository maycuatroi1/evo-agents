// The page Growth weekly of the core plugin marketing, framed by the hub at /p/<project>/x/marketing/weekly, the link
// of the Monday notice: each Goal's signal against its target, the posts that went out, the breakers, the cost against
// the month's cap, and the drafts kept for the blog project. Every member reads it.
(function () {
  "use strict";
  var evo = window.evo;
  var page = window.page;
  var marketing = page.marketing;
  var el = page.el;
  var t = page.t;

  var root = document.getElementById("page");

  function section(id, title, count, children) {
    return page.section("weekly-" + id, title, count === undefined ? null : el("span", { class: "evo-small evo-subtle", text: t("weekly.count", { count: count }) }), el("div", { class: "evo-stack-sm" }, children));
  }

  function empty(text) {
    return el("p", { class: "evo-small evo-muted", text: text });
  }

  function figure(value) {
    return value === null || value === undefined ? t("weekly.noValue") : page.number(value);
  }

  /** Each active Goal with a metric: its signal over the window against its target, and whether it is met. */
  function goals(week) {
    if (week.goals.length === 0) return section("goals", t("weekly.goals"), 0, empty(t("weekly.noGoals")));
    var heads = [
      el("th", { scope: "col", text: t("weekly.goal") }),
      el("th", { scope: "col", text: t("weekly.signal") }),
      el("th", { scope: "col", class: "page-right", text: t("weekly.value") }),
      el("th", { scope: "col", class: "page-right", text: t("weekly.target") }),
      el("th", { scope: "col", text: t("weekly.met") }),
    ];
    var table = el("div", { class: "evo-table-wrap", role: "region", tabindex: "0", "aria-label": t("weekly.goals") }, [
      el("table", { class: "evo-table" }, [
        el("thead", null, el("tr", null, heads)),
        el(
          "tbody",
          null,
          week.goals.map(function (goal) {
            var met = goal.reached === null ? page.badge(t("weekly.unknown"), "outline") : goal.reached ? page.badge(t("weekly.reached"), "success") : page.badge(t("weekly.notReached"), "warning");
            return el("tr", { testid: "weekly-goal" }, [
              el("td", { class: "evo-wrap" }, [el("span", { class: "evo-mono evo-small", text: goal.code }), " ", el("span", { class: "page-wrap", text: goal.statement })]),
              el("td", { class: "evo-wrap evo-mono evo-small page-wrap", text: goal.metric }),
              el("td", { class: "page-right page-num", text: figure(goal.value) }),
              el("td", { class: "page-right page-num", text: figure(goal.target) }),
              el("td", null, met),
            ]);
          }),
        ),
      ]),
    ]);
    return section("goals", t("weekly.goals"), week.goals.length, table);
  }

  function line(testId, parts) {
    return el("li", { testid: testId }, el("div", { class: "page-line" }, parts));
  }

  function posted(week) {
    return section(
      "posted",
      t("weekly.posted"),
      week.posted.length,
      week.posted.length === 0
        ? empty(t("weekly.noPosts"))
        : el(
            "ul",
            { class: "page-list" },
            week.posted.map(function (post) {
              return line("weekly-post", [
                el("span", { class: "page-strong", text: post.channel }),
                el("span", { class: "evo-mono evo-small evo-muted page-wrap", text: post.account }),
                post.posted_url ? page.external(post.posted_url, { class: "page-link evo-small" }, t("weekly.open")) : null,
                el("span", { class: "page-end evo-small evo-muted" }, page.when(post.posted_at)),
              ]);
            }),
          ),
    );
  }

  function breakers(week) {
    return section(
      "breakers",
      t("weekly.breakers"),
      week.breakers.length,
      week.breakers.length === 0
        ? empty(t("weekly.noBreakers"))
        : el(
            "ul",
            { class: "page-list" },
            week.breakers.map(function (item) {
              return el("li", { testid: "weekly-breaker" }, [
                el("div", { class: "page-line" }, [el("span", { class: "page-strong", text: item.channel }), el("span", { class: "evo-mono evo-small evo-muted page-wrap", text: item.account }), el("span", { class: "page-end evo-small evo-muted" }, page.when(item.paused_at))]),
                item.reason ? el("p", { class: "evo-small evo-muted page-wrap", text: item.reason }) : null,
              ]);
            }),
          ),
    );
  }

  function cost(week) {
    return section("cost", t("weekly.cost"), undefined, [
      el("p", { class: "evo-small" }, [t("weekly.weekCost") + " ", el("span", { class: "page-strong page-num", testid: "weekly-week-cost", text: page.money(week.week_cost_usd) })]),
      marketing.budgetMeter(week.budget),
    ]);
  }

  function proposals(week) {
    return section("proposals", week.proposals_project ? t("weekly.proposalsFor", { project: week.proposals_project }) : t("weekly.proposals"), week.proposals.length, [
      week.proposals_project ? null : empty(t("weekly.noProposalsProject")),
      week.proposals.length === 0
        ? empty(t("weekly.noProposals"))
        : el(
            "ul",
            { class: "page-list" },
            week.proposals.map(function (item) {
              return line("weekly-proposal", [
                el("span", { class: "page-strong", text: item.channel }),
                el("span", { class: "evo-mono evo-small evo-muted page-wrap", text: item.account }),
                page.badge(item.state, "outline"),
                el("span", { class: "page-end evo-small evo-muted" }, page.when(item.created_at)),
              ]);
            }),
          ),
    ]);
  }

  function week(data) {
    return el("div", { class: "evo-stack", testid: "weekly-report" }, [
      data.killed ? page.banner(t("weekly.killed"), data.reason, { testid: "weekly-killed" }, "danger") : null,
      goals(data),
      el("div", { class: "page-report" }, [posted(data), breakers(data), cost(data), proposals(data)]),
    ]);
  }

  function show() {
    var sub = el("p", { class: "evo-muted" });
    var body = el("div", { class: "evo-stack" });
    page.fill(root, [sub, marketing.tabs("weekly"), body]);
    return page.call("weekly").then(
      function (data) {
        sub.textContent = t("weekly.sub", { since: page.day(data.since), until: page.day(data.until) });
        page.fill(body, week(data));
        root.removeAttribute("aria-busy");
      },
      function (error) {
        page.fill(body, page.failed(error, show));
        root.removeAttribute("aria-busy");
      },
    );
  }

  evo.ready.then(function () {
    document.title = t("tabs.weekly");
    root.setAttribute("data-testid", "marketing-weekly");
    show();
  });
})();
