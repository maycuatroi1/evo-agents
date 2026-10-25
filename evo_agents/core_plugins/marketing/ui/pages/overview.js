// The page Marketing of the core plugin marketing, framed by the hub at /p/<project>/x/marketing/overview: the kill
// switch, the month's cost against its cap, and each channel account with its breaker, its health and, for a browser
// channel, its login session. Any writer stops all posting; only an admin resumes it or closes a breaker.
(function () {
  "use strict";
  var evo = window.evo;
  var page = window.page;
  var marketing = page.marketing;
  var el = page.el;
  var t = page.t;

  /** The reason the kill switch keeps when it is pressed on this page. */
  var KILL_REASON = "stopped from the web";
  var root = document.getElementById("page");
  var loading = 0;

  /** A switch of the whole project: Stop all posting for a writer (confirmed), and Resume for an admin once stopped. */
  function killSwitch(status) {
    var stopping = !status.killed;
    if (stopping ? !page.canWrite() : page.role() !== "admin") return null;
    return el(
      "button",
      {
        type: "button",
        class: stopping ? "evo-btn evo-btn-ghost page-danger" : "evo-btn evo-btn-secondary",
        testid: stopping ? "marketing-kill" : "marketing-unkill",
        on: {
          click: function () {
            page.confirm({
              testId: "marketing-kill-confirm",
              title: stopping ? t("kill.stopTitle") : t("kill.resumeTitle"),
              text: stopping ? t("kill.stopText") : t("kill.resumeText"),
              confirm: stopping ? t("kill.stopConfirm") : t("kill.resumeConfirm"),
              pending: t("kill.pending"),
              cancel: t("kill.cancel"),
              danger: stopping,
              byStatus: { 403: t("kill.forbidden") },
              run: function () {
                var write = stopping ? page.call("kill", { method: "POST", body: { reason: KILL_REASON } }) : page.call("resume", { method: "POST", body: {} });
                return write.then(function () {
                  evo.toast(stopping ? t("kill.stopped") : t("kill.resumed"), "success");
                  return show();
                });
              },
            });
          },
        },
      },
      stopping ? t("kill.stop") : t("kill.resume"),
    );
  }

  /** An admin's Close breaker on a paused channel: nothing closes it by itself. */
  function closeBreaker(channel) {
    var button = el("button", { type: "button", class: "evo-btn evo-btn-secondary evo-btn-sm", testid: "marketing-close-breaker", text: t("channels.close") });
    button.addEventListener("click", function () {
      button.setAttribute("aria-busy", "true");
      button.disabled = true;
      page.call("channels/" + encodeURIComponent(channel.id) + "/resume", { method: "POST", body: {} }).then(
        function () {
          evo.toast(t("channels.closed", { channel: channel.channel, account: channel.account }), "success");
          return show();
        },
        function (error) {
          button.removeAttribute("aria-busy");
          button.disabled = false;
          evo.toast(page.failure(error), "error");
        },
      );
    });
    return button;
  }

  function stack(children, props) {
    return el("div", Object.assign({ class: "page-cell" }, props || {}), children);
  }

  function session(channel) {
    if (channel.adapter !== "browser") return el("span", { class: "evo-small evo-muted", text: t("channels.noSession") });
    if (channel.session_state === "stale") return page.badge(t("channels.stale"), "warning", { testid: "marketing-session-stale" });
    if (channel.session_state === "ok") return page.badge(t("channels.sessionOk"), "success");
    return page.badge(t("channels.sessionNone"), "outline");
  }

  function health(channel) {
    var variant = channel.health === "ok" ? "success" : channel.health === "failing" ? "danger" : "outline";
    var word = channel.health === "ok" ? t("channels.healthOk") : channel.health === "failing" ? t("channels.healthFailing") : channel.health === "n/a" ? t("channels.healthNa") : t("channels.healthUnknown");
    return stack([page.badge(word, variant), channel.health_reason ? el("span", { class: "evo-small evo-muted page-wrap", text: channel.health_reason }) : null], { testid: "marketing-health", "data-health": channel.health });
  }

  function channelTable(channels) {
    if (channels.length === 0) {
      return el("div", { class: "evo-empty", testid: "marketing-channels-empty" }, [el("p", { class: "evo-empty-title", text: t("channels.emptyTitle") }), el("p", { class: "evo-muted", text: t("channels.emptyText") })]);
    }
    var admin = page.role() === "admin";
    var heads = ["channel", "mode", "breaker", "session", "health"].map(function (key) {
      return el("th", { scope: "col", text: t("channels." + key) });
    });
    if (admin) heads.push(el("th", { scope: "col" }, el("span", { class: "evo-sr-only", text: t("channels.actions") })));
    return el("div", { class: "evo-table-wrap", role: "region", tabindex: "0", "aria-label": t("channels.tableLabel") }, [
      el("table", { class: "evo-table", testid: "marketing-channels" }, [
        el("thead", null, el("tr", null, heads)),
        el(
          "tbody",
          null,
          channels.map(function (channel) {
            return el("tr", { testid: "marketing-channel-row", "data-paused": String(channel.paused) }, [
              el("td", { class: "evo-wrap" }, stack([el("span", { class: "page-strong", text: channel.channel }), el("span", { class: "evo-mono evo-small evo-muted page-wrap", text: channel.account + (channel.owner ? " (" + t("channels.ownedBy", { owner: channel.owner }) + ")" : "") })])),
              el("td", null, page.badge(channel.mode === "autonomous" ? t("channels.autonomous") : t("channels.draftOnly"), channel.mode === "autonomous" ? "info" : "outline")),
              el(
                "td",
                { class: "evo-wrap" },
                channel.paused
                  ? stack([page.badge(t("channels.open"), "danger"), channel.paused_reason ? el("span", { class: "evo-small evo-muted page-wrap", text: channel.paused_reason }) : null], { testid: "marketing-breaker-open" })
                  : page.badge(t("channels.shut"), "success"),
              ),
              el("td", null, session(channel)),
              el("td", { class: "evo-wrap" }, health(channel)),
              admin ? el("td", null, channel.paused ? closeBreaker(channel) : null) : null,
            ]);
          }),
        ),
      ]),
    ]);
  }

  function fact(label, value, props) {
    return el("div", null, [el("dt", { class: "evo-small evo-muted", text: label }), el("dd", Object.assign({ text: String(value) }, props))]);
  }

  function overview(status, channels) {
    return [
      status.killed ? page.banner(t("kill.killedTitle"), status.reason ? t("kill.killedReason", { reason: status.reason }) : t("kill.killedText"), { testid: "marketing-killed" }, "danger") : null,
      el("div", { class: "page-overview" }, [
        page.section(
          "marketing-month",
          t("month"),
          null,
          el("div", { class: "evo-stack" }, [
            marketing.budgetMeter(status.budget),
            el("dl", { class: "page-facts page-facts-2" }, [
              fact(t("channelCount"), status.channels, { class: "page-count", testid: "marketing-channel-count" }),
              fact(t("breakerCount"), status.open_breakers, { class: "page-count" + (status.open_breakers > 0 ? " page-danger" : ""), testid: "marketing-breaker-count" }),
            ]),
          ]),
          { testid: null },
        ),
        page.section("marketing-channels-card", t("channels.title"), null, channelTable(channels), { testid: null }),
      ]),
    ];
  }

  function show() {
    loading += 1;
    var token = loading;
    root.setAttribute("aria-busy", "true");
    return Promise.all([page.call("status"), page.call("channels")]).then(
      function (answers) {
        if (token !== loading) return;
        var status = answers[0];
        page.fill(root, [
          el("header", { class: "page-head" }, [
            el("div", { class: "evo-row evo-grow" }, [el("p", { class: "evo-muted evo-grow", text: t("sub") }), status.killed ? page.badge(t("kill.killedBadge"), "danger") : null]),
            killSwitch(status),
          ]),
          marketing.tabs("overview"),
          page.role() === "reader" ? marketing.readerNote() : null,
          overview(status, answers[1]),
        ]);
        root.removeAttribute("aria-busy");
      },
      function (error) {
        if (token !== loading) return;
        page.fill(root, [el("p", { class: "evo-muted", text: t("sub") }), marketing.tabs("overview"), page.failed(error, show)]);
        root.removeAttribute("aria-busy");
      },
    );
  }

  evo.ready.then(function () {
    document.title = t("tabs.overview");
    page.fill(root, [el("p", { class: "evo-muted", text: t("sub") }), marketing.tabs("overview")]);
    show();
  });
})();
