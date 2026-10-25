// The page PM daily of the core plugin pm, framed by the hub at /p/<project>/x/pm/daily: the eight sections of one
// day, today in the project's timezone unless ?date= names another. Each task links to its page in the tasks plugin;
// each blocker names who or what it waits on and its age. Its words are in i18n/<locale>.js.
(function () {
  "use strict";
  var evo = window.evo;
  var page = window.page;
  var el = page.el;
  var t = page.t;

  var root = document.getElementById("page");
  var loading = 0;

  /** A day as the route takes it, YYYY-MM-DD; null for anything else, so a hand-written URL asks for today. */
  function validDate(value) {
    if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    return isNaN(Date.parse(value + "T00:00:00Z")) ? null : value;
  }

  function picked() {
    var query = (evo.context.location && evo.context.location.query) || {};
    return validDate(query.date);
  }

  function projectPath(rest) {
    return "/p/" + encodeURIComponent(evo.context.project) + "/" + rest;
  }

  /** A task's ref as a link to its page in the tasks plugin. */
  function taskRef(ref) {
    return page.link(page.pagePath("tasks", "board", encodeURIComponent(ref)), { class: "page-link evo-mono", testid: "pm-task-link" }, ref);
  }

  function percent(value) {
    return value === null || value === undefined ? "-" : page.number(value / 100, { style: "percent", maximumFractionDigits: 0 });
  }

  /** One row: the task's ref and title, then what the section says of it. */
  function row(ref, title, testId, after) {
    return el("li", { testid: testId }, [
      el("div", { class: "page-line" }, [taskRef(ref), el("span", { class: "evo-grow page-wrap", text: title }), after ? el("span", { class: "evo-small evo-muted" }, after) : null]),
    ]);
  }

  /**
   * A section of the report: its heading, how many it holds, and its rows, or the line that says it is empty. `hidden`
   * counts what the reader may not see, which only adds to the count.
   */
  function section(id, title, count, hidden, empty, rows) {
    return page.section(
      "pm-" + id,
      title,
      el("span", { class: "evo-small evo-subtle", testid: "pm-" + id + "-count", text: t("count", { count: count }) }),
      el("div", { class: "evo-stack-sm" }, [
        count === 0 ? el("p", { class: "evo-small evo-muted", testid: "pm-" + id + "-empty", text: empty }) : el("ul", { class: "page-list" }, rows),
        hidden > 0 ? el("p", { class: "evo-small evo-muted", testid: "pm-" + id + "-hidden", text: t("hidden", { count: hidden }) }) : null,
      ]),
    );
  }

  function withPeople(text, people) {
    return people.length ? text + " - " + people.join(", ") : text;
  }

  function done(daily) {
    var rows = daily.done_use_cases
      .map(function (item) {
        return el("li", { testid: "pm-done-use-case" }, [
          el("div", { class: "page-line" }, [el("span", { class: "evo-mono evo-small", text: item.code }), el("span", { class: "evo-grow page-wrap page-strong", text: item.title }), taskRef(item.task)]),
          item.outcome ? el("p", { class: "evo-small evo-muted page-wrap", text: item.outcome }) : null,
        ]);
      })
      .concat(
        daily.done_tasks.map(function (item) {
          return row(item.ref, item.title, "pm-done-task", page.when(item.done_at));
        }),
      );
    var count = daily.done_use_cases.length + daily.done_tasks.length;
    return section("done", t("done.title", { day: daily.previous_working_day }), count, daily.hidden.done_use_cases + daily.hidden.done_tasks, t("done.empty"), rows);
  }

  function fact(label, value) {
    return el("div", null, [el("dt", { class: "evo-small evo-muted", text: label }), el("dd", { class: "page-fact", text: value })]);
  }

  function sprint(daily) {
    var current = daily.sprint;
    var rows = current
      ? [
          el("li", { testid: "pm-sprint-body", class: "evo-stack-sm" }, [
            el("p", { class: "page-strong page-wrap", text: current.name }),
            el("dl", { class: "page-facts" }, [
              fact(t("sprint.done"), percent(current.done_percent)),
              fact(t("sprint.time"), percent(current.time_percent)),
              fact(t("sprint.scope"), String(current.scope)),
              fact(t("sprint.changed"), t("sprint.changedValue", { added: current.added, removed: current.removed })),
            ]),
            current.use_cases.length
              ? el(
                  "ul",
                  { class: "page-list evo-small", "aria-label": t("sprint.useCases") },
                  current.use_cases.map(function (item) {
                    return el("li", { testid: "pm-sprint-use-case" }, el("div", { class: "page-line" }, [el("span", { class: "evo-mono", text: item.code }), el("span", { class: "evo-grow page-wrap", text: item.title }), el("span", { class: "evo-muted", text: t("sprint.ratio", { done: item.done, total: item.total }) })]));
                  }),
                )
              : null,
          ]),
        ]
      : [];
    return section("sprint", t("sprint.title"), current ? 1 : 0, daily.hidden.sprint, t("sprint.empty"), rows);
  }

  function late(daily) {
    return section(
      "late",
      t("late.title"),
      daily.late.length,
      daily.hidden.late,
      t("late.empty"),
      daily.late.map(function (item) {
        var text = item.kind === "overdue" ? t("late.overdue", { due: item.due_on, days: item.days_late || 0 }) : t("late.atRisk", { due: item.due_on, days: item.days_left || 0 });
        return row(item.ref, item.title, "pm-late-row", withPeople(text, item.assignees));
      }),
    );
  }

  function review(daily) {
    return section(
      "review",
      t("review.title"),
      daily.review.length,
      daily.hidden.review,
      t("review.empty"),
      daily.review.map(function (item) {
        var text = t("review.age", { days: item.age_days });
        if (item.qa.length) text += " - " + t("review.qa", { who: item.qa.join(", ") });
        return row(item.ref, item.title, "pm-review-row", text);
      }),
    );
  }

  function stale(daily) {
    return section(
      "stale",
      t("stale.title"),
      daily.stale.length,
      daily.hidden.stale,
      t("stale.empty"),
      daily.stale.map(function (item) {
        return row(item.ref, item.title, "pm-stale-row", withPeople(t("stale.idle", { days: item.idle_days }), item.assignees));
      }),
    );
  }

  /** Who or what a blocker waits on, with its age: the member, a task as a link, or the words given. */
  function waiting(blocker) {
    var on =
      blocker.waiting_kind === "task"
        ? [t("blocked.waitingTask") + " ", taskRef(blocker.waiting_on)]
        : [(blocker.waiting_kind === "member" ? t("blocked.waitingMember") : t("blocked.waitingText")) + " ", el("span", { class: "page-strong page-fg page-wrap", text: blocker.waiting_on })];
    return on.concat([" - ", el("span", { testid: "pm-blocker-age", text: t("blocked.age", { days: blocker.age_days }) })]);
  }

  function blockerRow(blocker, testId) {
    return el("li", { testid: testId }, [
      el("div", { class: "page-line" }, [taskRef(blocker.ref), el("span", { class: "evo-grow page-wrap", text: blocker.title }), el("span", { class: "evo-small evo-muted", text: blocker.kind === "decision" ? t("blocked.byDecision") : t("blocked.byHand") })]),
      el("p", { class: "evo-small evo-muted" }, waiting(blocker)),
      blocker.reason ? el("p", { class: "evo-small page-wrap", text: blocker.reason }) : null,
    ]);
  }

  function blocked(daily) {
    return section(
      "blocked",
      t("blocked.title"),
      daily.blocked.length,
      daily.hidden.blocked,
      t("blocked.empty"),
      daily.blocked.map(function (item) {
        return blockerRow(item, "pm-blocked-row");
      }),
    );
  }

  function escalate(daily) {
    return section(
      "escalate",
      t("escalate.title"),
      daily.escalate.length,
      daily.hidden.escalate,
      t("escalate.empty"),
      daily.escalate.map(function (item) {
        return blockerRow(item, "pm-escalate-row");
      }),
    );
  }

  function load(daily) {
    return section(
      "load",
      t("load.title"),
      daily.load.length,
      0,
      t("load.empty"),
      daily.load.map(function (item) {
        return el("li", { testid: "pm-load-row", "data-overloaded": String(item.overloaded), "data-empty": String(item.empty) }, [
          el("div", { class: "page-line" }, [
            el("span", { class: "page-strong page-wrap", text: item.login }),
            el("span", { class: "evo-small evo-muted", text: item.role }),
            item.overloaded ? page.badge(t("load.overloaded"), "danger", { testid: "pm-overloaded" }) : null,
            item.empty ? page.badge(t("load.free"), "outline", { testid: "pm-free" }) : null,
          ]),
          el("p", { class: "evo-small evo-muted", text: t("load.counts", { doing: item.doing, limit: item.wip_limit, review: item.review_as_qa, late: item.late }) }),
          item.doing_tasks.length ? el("p", { class: "page-line evo-small" }, item.doing_tasks.map(taskRef)) : null,
        ]);
      }),
    );
  }

  /** A project with no task at all has nothing to report: the way to its first one. */
  function isBare(daily) {
    var counted = daily.done_use_cases.length + daily.done_tasks.length + daily.late.length + daily.review.length + daily.stale.length + daily.blocked.length + daily.escalate.length;
    var idle = daily.load.every(function (item) {
      return item.doing === 0 && item.late === 0 && item.review_as_qa === 0;
    });
    var hidden = Object.keys(daily.hidden).every(function (key) {
      return daily.hidden[key] === 0;
    });
    return daily.sprint === null && counted === 0 && idle && hidden;
  }

  function report(daily) {
    if (isBare(daily)) {
      return el("div", { class: "evo-empty", testid: "pm-bare" }, [
        el("p", { class: "evo-empty-title", text: t("bareTitle") }),
        el("p", { class: "evo-muted", text: t("bareText") }),
        page.link(page.pagePath("tasks", "board"), { class: "evo-btn evo-btn-secondary" }, t("openBoard")),
      ]);
    }
    return el("div", { class: "page-report", testid: "pm-report" }, [done(daily), sprint(daily), blocked(daily), escalate(daily), late(daily), review(daily), stale(daily), load(daily)]);
  }

  /** The 409 of the route: tasks not enabled, or a config of pm the hub refuses. Says which, and where to fix it. */
  function notReady(error) {
    return el("div", { class: "page-banner page-banner-danger evo-stack-sm", role: "alert", testid: "pm-not-ready" }, [
      el("p", { class: "page-banner-title", text: t("notReady.title") }),
      el("p", { class: "page-banner-text", text: t("notReady.text") }),
      el("p", { class: "evo-small evo-muted page-wrap", testid: "pm-not-ready-reason", text: error.message }),
      el("div", { class: "evo-row" }, [
        page.link(projectPath("plugins/tasks"), { class: "evo-btn evo-btn-secondary" }, t("notReady.tasks")),
        page.link(projectPath("plugins/pm"), { class: "evo-btn evo-btn-secondary" }, t("notReady.config")),
      ]),
    ]);
  }

  function header(daily) {
    var date = picked();
    var input = el("input", { id: "pm-date-input", type: "date", class: "evo-input page-date", testid: "pm-date" });
    input.value = date || (daily ? daily.date : "");
    input.addEventListener("change", function () {
      var next = validDate(input.value);
      evo.navigate(next ? "?date=" + next : evo.href(evo.context.location.path)).catch(function () {});
    });
    return el("header", { class: "page-head" }, [
      el("p", { class: "evo-muted evo-grow" }, daily ? t("sub", { date: daily.date, previous: daily.previous_working_day }) : null),
      el("div", { class: "evo-row" }, [
        el("label", { for: "pm-date-input", class: "evo-label", text: t("day") }),
        input,
        date
          ? el("button", {
              type: "button",
              class: "evo-btn evo-btn-secondary",
              testid: "pm-today",
              text: t("today"),
              on: {
                click: function () {
                  evo.navigate(evo.href(evo.context.location.path)).catch(function () {});
                },
              },
            })
          : null,
      ]),
    ]);
  }

  function show() {
    loading += 1;
    var token = loading;
    var date = picked();
    root.setAttribute("aria-busy", "true");
    root.setAttribute("data-testid", "pm-daily");
    page.fill(root, header(null));
    return page.call("daily", { query: date ? { date: date } : {} }).then(
      function (daily) {
        if (token !== loading) return;
        page.fill(root, [header(daily), report(daily)]);
        root.removeAttribute("aria-busy");
      },
      function (error) {
        if (token !== loading) return;
        page.fill(root, [header(null), error.status === 409 ? notReady(error) : page.failed(error, show)]);
        root.removeAttribute("aria-busy");
      },
    );
  }

  evo.ready.then(function () {
    document.title = t("day");
    show();
    evo.on("location", show);
  });
})();
