// The page Posts of the core plugin marketing, framed by the hub at /p/<project>/x/marketing/posts, for its writers:
// the posts grouped by what the gate did with them, drafts first, each draft with a Copy button for the member who
// shares it by hand (a personal account, a Facebook group). A reader reads no post's text: a note says so.
(function () {
  "use strict";
  var evo = window.evo;
  var page = window.page;
  var marketing = page.marketing;
  var el = page.el;
  var t = page.t;

  /** The groups of the gate's result, in the order a writer looks at them, and the post states each holds. */
  var GROUPS = [
    { id: "drafts", states: ["draft"] },
    { id: "waiting", states: ["proposed", "judging"] },
    { id: "passed", states: ["queued", "posting"] },
    { id: "posted", states: ["posted"] },
    { id: "stopped", states: ["failed", "withdrawn"] },
  ];
  var STATE_VARIANT = { proposed: "outline", judging: "outline", queued: "info", posting: "info", posted: "success", draft: "warning", failed: "danger", withdrawn: "outline" };
  var root = document.getElementById("page");

  function select(node) {
    var selection = window.getSelection();
    if (!selection) return false;
    var range = document.createRange();
    range.selectNodeContents(node);
    selection.removeAllRanges();
    selection.addRange(range);
    return true;
  }

  /**
   * Copies the draft's text. The frame's sandbox gives its page no clipboard permission, so navigator.clipboard is
   * tried first and the browser's copy of the selected text next; where both are refused the text stays selected for
   * the member's own Ctrl+C or Cmd+C, and the toast says so.
   */
  function copyDraft(post, body) {
    var button = el("button", { type: "button", class: "evo-btn evo-btn-secondary evo-btn-sm", testid: "marketing-copy", text: t("posts.copy") });
    var reset = null;
    function copied() {
      button.textContent = t("posts.copiedShort");
      evo.toast(t("posts.copied"), "success");
      clearTimeout(reset);
      reset = setTimeout(function () {
        button.textContent = t("posts.copy");
      }, 2000);
    }
    function fallback() {
      var done = false;
      if (select(body)) {
        try {
          done = document.execCommand("copy");
        } catch (error) {
          done = false;
        }
      }
      if (done) copied();
      else evo.toast(t("posts.selected"), "info");
    }
    button.addEventListener("click", function () {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(post.body).then(copied, fallback);
      } else fallback();
    });
    return button;
  }

  function postCard(post) {
    var body = el("p", { class: "evo-small page-wrap", testid: "marketing-post-body", text: post.body });
    var known = Object.prototype.hasOwnProperty.call(STATE_VARIANT, post.state);
    return el("li", { testid: "marketing-post", "data-state": post.state }, [
      el("div", { class: "page-line" }, [
        el("span", { class: "page-strong", text: post.channel }),
        el("span", { class: "evo-mono evo-small evo-muted page-wrap", text: post.account }),
        page.badge(known ? t("posts.states." + post.state) : post.state, STATE_VARIANT[post.state] || "outline"),
        post.dry_run ? page.badge(t("posts.dryRun"), "outline") : null,
        el("span", { class: "page-end evo-small evo-muted" }, page.when(post.posted_at || post.created_at)),
      ]),
      body,
      post.reason ? el("p", { class: "evo-small evo-muted page-wrap", testid: "marketing-post-reason", text: t("posts.reason", { reason: post.reason }) }) : null,
      el("div", { class: "evo-row" }, [
        post.state === "draft" ? copyDraft(post, body) : null,
        post.posted_url ? page.external(post.posted_url, { class: "evo-btn evo-btn-ghost evo-btn-sm" }, t("posts.open")) : null,
        post.cost_usd > 0 ? el("span", { class: "evo-small evo-muted", text: t("posts.cost", { cost: page.money(post.cost_usd) }) }) : null,
      ]),
    ]);
  }

  function group(id, posts) {
    var titleId = "marketing-" + id + "-title";
    return el("section", { class: "evo-card", role: "region", "aria-labelledby": titleId, testid: "marketing-group-" + id }, [
      el("div", null, [
        el("div", { class: "page-section-head" }, [el("h2", { id: titleId, class: "evo-card-title", text: t("posts.groups." + id) }), el("span", { class: "evo-small evo-subtle", text: t("posts.count", { count: posts.length }) })]),
        el("p", { class: "evo-small evo-muted", text: t("posts.groupText." + id) }),
      ]),
      posts.length === 0 ? el("p", { class: "evo-small evo-muted", text: t("posts.none") }) : el("ul", { class: "page-list" }, posts.map(postCard)),
    ]);
  }

  function list(posts) {
    if (posts.length === 0) {
      return el("div", { class: "evo-empty", testid: "marketing-posts-empty" }, [el("p", { class: "evo-empty-title", text: t("posts.emptyTitle") }), el("p", { class: "evo-muted", text: t("posts.emptyText") })]);
    }
    return el(
      "div",
      { class: "evo-stack", testid: "marketing-posts" },
      GROUPS.map(function (each) {
        return group(
          each.id,
          posts.filter(function (post) {
            return each.states.indexOf(post.state) >= 0;
          }),
        );
      }),
    );
  }

  function show() {
    var body = el("div", { class: "evo-stack" });
    page.fill(root, [el("p", { class: "evo-muted", text: t("posts.sub") }), marketing.tabs("posts"), body]);
    if (!page.canWrite()) {
      page.fill(body, marketing.readerNote(t("posts.readerText")));
      root.removeAttribute("aria-busy");
      return Promise.resolve();
    }
    return page.call("posts").then(
      function (posts) {
        page.fill(body, list(posts));
        root.removeAttribute("aria-busy");
      },
      function (error) {
        page.fill(body, page.failed(error, show));
        root.removeAttribute("aria-busy");
      },
    );
  }

  evo.ready.then(function () {
    document.title = t("tabs.posts");
    root.setAttribute("data-testid", "marketing-posts-page");
    show();
  });
})();
