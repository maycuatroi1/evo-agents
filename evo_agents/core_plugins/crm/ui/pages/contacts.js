// The page Contacts of the core plugin crm, framed by the hub at /p/<project>/x/crm/contacts: the people who found
// the project themselves, the pipeline by stage, then the list. Its sub path <login> shows one contact: its stage and
// source, its touchpoints newest first, the stages it reached, a writer's note and an admin's Delete on that person's
// request. A reader sees a note that they only read. Its words are in i18n/<locale>.js.
(function () {
  "use strict";
  var evo = window.evo;
  var page = window.page;
  var el = page.el;
  var t = page.t;

  /** The stages in the order a contact moves through them (evo_agents.core_plugins.crm STAGES). */
  var STAGES = ["lead", "invited", "activated", "retained"];
  /** Where a contact came from and what a touchpoint is. */
  var KINDS = ["invite_request", "invite_link", "grant", "issue", "pull_request", "discussion", "note"];
  /** The badge of each stage: lead plain, invited blue, activated and retained green; its word is always written. */
  var STAGE_VARIANT = { lead: "outline", invited: "info", activated: "success", retained: "success" };
  /** The longest note the hub keeps. */
  var MAX_NOTE = 1000;

  var root = document.getElementById("page");
  var picked = null;
  var shown = null;
  var loading = 0;

  function kindWord(kind) {
    return KINDS.indexOf(kind) >= 0 ? t("kinds." + kind) : kind;
  }

  function stageBadge(stage) {
    var known = STAGES.indexOf(stage) >= 0;
    return page.badge(known ? t("stages." + stage) : stage, known ? STAGE_VARIANT[stage] : "outline", { testid: "crm-stage" });
  }

  function readerNote() {
    return page.role() === "reader" ? page.banner(t("readerTitle", { project: evo.context.project }), t("readerText"), { testid: "crm-read-only" }) : null;
  }

  function contactsPath() {
    return page.pagePath("crm", "contacts");
  }

  function contactPath(login) {
    return page.pagePath("crm", "contacts", encodeURIComponent(login));
  }

  // The list ---------------------------------------------------------------------------------------------------

  function pipeline(data, redraw) {
    var cells = [{ id: null, label: t("all"), count: data.contacts.length }].concat(
      STAGES.map(function (stage) {
        return { id: stage, label: t("stages." + stage), count: (data.counts && data.counts[stage]) || 0 };
      }),
    );
    return el("section", { "aria-labelledby": "crm-pipeline-title", class: "evo-stack-sm" }, [
      el("h2", { id: "crm-pipeline-title", class: "evo-card-title", text: t("pipeline") }),
      el(
        "div",
        { role: "group", "aria-label": t("pipelineFilter"), class: "page-filter", testid: "crm-pipeline" },
        cells.map(function (cell) {
          return el(
            "button",
            {
              type: "button",
              "aria-pressed": picked === cell.id ? "true" : "false",
              testid: "crm-stage-" + (cell.id || "all"),
              on: {
                click: function () {
                  picked = cell.id;
                  redraw();
                },
              },
            },
            [el("span", { class: "evo-small evo-muted", text: cell.label }), el("span", { class: "page-filter-count", testid: "crm-stage-count", text: String(cell.count) })],
          );
        }),
      ),
    ]);
  }

  function contactTable(data) {
    var rows = picked
      ? data.contacts.filter(function (contact) {
          return contact.stage === picked;
        })
      : data.contacts;
    if (rows.length === 0) {
      return el("div", { class: "evo-empty", testid: "crm-empty" }, [
        el("p", { class: "evo-empty-title", text: picked ? t("emptyStage", { stage: t("stages." + picked) }) : t("emptyTitle") }),
        el("p", { class: "evo-muted", text: t("emptyText") }),
      ]);
    }
    var head = ["login", "stage", "source", "firstSeen", "stageAt"].map(function (column) {
      return el("th", { scope: "col", text: t("columns." + column) });
    });
    return el("div", { class: "evo-card evo-card-sm" }, [
      el("div", { class: "evo-table-wrap", role: "region", tabindex: "0", "aria-label": t("tableLabel") }, [
        el("table", { class: "evo-table", testid: "crm-contacts" }, [
          el("thead", null, el("tr", null, head)),
          el(
            "tbody",
            null,
            rows.map(function (contact) {
              return el("tr", { testid: "crm-contact-row" }, [
                el("td", { class: "evo-wrap" }, page.link(contactPath(contact.login), { class: "page-link evo-mono", testid: "crm-contact-link" }, contact.login)),
                el("td", null, stageBadge(contact.stage)),
                el("td", { text: kindWord(contact.source) }),
                el("td", null, page.when(contact.first_seen_at)),
                el("td", null, page.when(contact.stage_at)),
              ]);
            }),
          ),
        ]),
      ]),
    ]);
  }

  function showList(token) {
    var body = el("div", { class: "evo-stack" });
    page.fill(root, [el("p", { class: "evo-muted", text: t("sub") }), readerNote(), body]);
    root.setAttribute("data-testid", "crm-contacts-page");
    return page.call("contacts").then(
      function (data) {
        if (token !== loading) return;
        var redraw = function () {
          page.fill(body, [pipeline(data, redraw), contactTable(data)]);
        };
        redraw();
      },
      function (error) {
        if (token === loading) page.fill(body, page.failed(error, load));
      },
    );
  }

  // One contact ------------------------------------------------------------------------------------------------

  function noteForm(login) {
    var id = "crm-note-input";
    var error = el("p", { class: "evo-error", role: "alert", hidden: true });
    var save = el("button", { type: "submit", class: "evo-btn evo-btn-secondary", disabled: true, testid: "crm-note-save", text: t("detail.addNote") });
    var text = el("textarea", { id: id, class: "evo-input", rows: "3", maxlength: String(MAX_NOTE), testid: "crm-note" });
    var busy = false;
    text.addEventListener("input", function () {
      save.disabled = busy || !text.value.trim();
    });
    var form = el("form", { class: "evo-stack-sm page-note-form", testid: "crm-note-form" }, [
      el("label", { class: "evo-label", for: id, text: t("detail.noteLabel") }),
      text,
      error,
      el("div", null, save),
    ]);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var note = text.value.trim();
      if (!note || busy) return;
      busy = true;
      save.disabled = true;
      save.setAttribute("aria-busy", "true");
      save.textContent = t("detail.noting");
      error.hidden = true;
      page
        .call("contacts/" + encodeURIComponent(login) + "/notes", { method: "POST", body: { note: note } })
        .then(
          function () {
            text.value = "";
            evo.toast(t("detail.noted"), "success");
            return load();
          },
          function (cause) {
            error.textContent = page.failure(cause);
            error.hidden = false;
          },
        )
        .then(function () {
          busy = false;
          save.removeAttribute("aria-busy");
          save.textContent = t("detail.addNote");
          save.disabled = !text.value.trim();
        });
    });
    return form;
  }

  function deleteButton(login) {
    return el(
      "button",
      {
        type: "button",
        class: "evo-btn evo-btn-ghost page-danger",
        testid: "crm-delete",
        on: {
          click: function () {
            page.confirm({
              testId: "crm-delete-confirm",
              title: t("detail.deleteTitle", { login: login }),
              text: t("detail.deleteText"),
              confirm: t("detail.deleteConfirm"),
              pending: t("detail.deleting"),
              cancel: t("detail.keep"),
              danger: true,
              byStatus: { 403: t("detail.deleteForbidden") },
              run: function () {
                return page.call("contacts/" + encodeURIComponent(login), { method: "DELETE" }).then(function () {
                  evo.toast(t("detail.deleted", { login: login }), "success");
                  return evo.navigate(contactsPath());
                });
              },
            });
          },
        },
      },
      t("detail.delete"),
    );
  }

  function touchpoint(point) {
    var ref = null;
    if (point.url) ref = page.external(point.url, { class: "page-link evo-mono evo-small" }, point.ref);
    else if (point.kind !== "note") ref = el("span", { class: "evo-mono evo-small page-wrap", text: point.ref });
    return el("li", { testid: "crm-touchpoint" }, [
      el("div", { class: "page-line" }, [el("span", { class: "page-strong", text: kindWord(point.kind) }), ref, el("span", { class: "page-end evo-small evo-muted" }, page.when(point.happened_at))]),
      point.note
        ? el("p", { class: "evo-small page-wrap" }, [point.note, point.author ? el("span", { class: "evo-muted", text: " " + t("detail.by", { author: point.author }) }) : null])
        : null,
    ]);
  }

  function detail(contact) {
    var role = page.role();
    var points = contact.touchpoints.slice().sort(function (a, b) {
      return b.happened_at < a.happened_at ? -1 : b.happened_at > a.happened_at ? 1 : 0;
    });
    var header = el("header", { class: "page-head", testid: "crm-contact-header" }, [
      el("div", { class: "evo-grow" }, [
        el("h2", null, [el("span", { class: "evo-mono", text: contact.login }), stageBadge(contact.stage)]),
        el("p", { class: "evo-muted", text: t("detail.sub", { source: kindWord(contact.source) }) }),
      ]),
      el("div", { class: "evo-row" }, [
        page.external(contact.profile_url, { class: "evo-btn evo-btn-secondary", testid: "crm-profile" }, t("detail.profile")),
        role === "admin" ? deleteButton(contact.login) : null,
      ]),
    ]);
    var touchpoints = page.section(
      "crm-touchpoints",
      t("detail.touchpoints"),
      null,
      el("div", { class: "evo-stack-sm" }, [
        points.length === 0 ? el("p", { class: "evo-small evo-muted", text: t("detail.noTouchpoints") }) : el("ol", { class: "page-list" }, points.map(touchpoint)),
        page.canWrite() ? noteForm(contact.login) : null,
      ]),
    );
    var history = page.section(
      "crm-history",
      t("detail.history"),
      null,
      el(
        "ol",
        { class: "page-list" },
        contact.history.map(function (step) {
          return el("li", { testid: "crm-history-step" }, el("div", { class: "evo-row evo-row-between" }, [stageBadge(step.stage), el("span", { class: "evo-small evo-muted" }, page.when(step.reached_at))]));
        }),
      ),
    );
    return [header, readerNote(), el("div", { class: "page-columns" }, [touchpoints, history])];
  }

  function showContact(login, token) {
    var body = el("div", { class: "evo-stack" });
    page.fill(root, [page.link(contactsPath(), { class: "evo-small evo-muted page-back", testid: "crm-back" }, "← " + t("back")), body]);
    root.setAttribute("data-testid", "crm-contact-page");
    return page.call("contacts/" + encodeURIComponent(login)).then(
      function (contact) {
        if (token === loading) page.fill(body, detail(contact));
      },
      function (error) {
        if (token === loading) page.fill(body, page.failed(error, load));
      },
    );
  }

  // Which of the two the sub path asks for ---------------------------------------------------------------------

  function login() {
    var path = (evo.context.location && evo.context.location.path) || "";
    var first = path.replace(/^\/+/, "").split("/")[0];
    return first ? decodeURIComponent(first) : null;
  }

  function load() {
    loading += 1;
    var token = loading;
    var who = login();
    if (who !== shown) picked = null;
    shown = who;
    root.setAttribute("aria-busy", "true");
    var done = who ? showContact(who, token) : showList(token);
    return done.then(function () {
      if (token === loading) root.removeAttribute("aria-busy");
    });
  }

  evo.ready.then(function () {
    document.title = t("tableLabel");
    load();
    evo.on("location", function () {
      load();
    });
  });
})();
