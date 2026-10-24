import { describe, expect, it } from "vitest";

import {
  answerAccess,
  answerBody,
  answerProblem,
  bellNumber,
  chosenOption,
  daysBefore,
  dayKey,
  groupInbox,
  inboxQuery,
  initialOption,
  inboxSearch,
  isFiltered,
  mergeAsk,
  noticeFacts,
  NO_FILTERS,
  notificationOfDecision,
  parkTiming,
  readInboxFilters,
  runLinkWords,
  notificationOfProposal,
  opensByItself,
  snippet,
  splitInbox,
  unreadIds,
} from "./model";
import { MAX_ANSWER_BYTES, type Notification } from "./queries";

const note = (id: number, extra: Partial<Notification> = {}): Notification => ({
  id,
  kind: "notice",
  notice_kind: "push_default_branch",
  project: "demo",
  run_id: 12,
  decision_id: null,
  decision_state: null,
  title: `Notice ${id}`,
  body: null,
  details: null,
  link: "/p/demo/runs/12",
  created_at: "2026-10-05T07:00:00Z",
  read_at: null,
  ...extra,
});

const decision = (id: number, state: Notification["decision_state"], extra: Partial<Notification> = {}) =>
  note(id, { kind: "decision", notice_kind: null, decision_id: 100 + id, decision_state: state, link: `/inbox?decision=${100 + id}`, ...extra });

describe("inbox filters", () => {
  it("reads the URL's filters, keeping only values the API takes", () => {
    const params = new URLSearchParams("kind=decision&project=demo&unread=1&page=3&decision=42");
    expect(readInboxFilters(params)).toEqual({ ...NO_FILTERS, kind: "decision", project: "demo", unread: true, page: 3, decision: 42 });
    expect(readInboxFilters(new URLSearchParams("q=%20deploy%20staging%20&run=707"))).toEqual({ ...NO_FILTERS, q: "deploy staging", run: 707 });
    expect(readInboxFilters(new URLSearchParams(`q=${"x".repeat(250)}&run=-1`))).toEqual({ ...NO_FILTERS, q: "x".repeat(200) });
    expect(readInboxFilters(new URLSearchParams("kind=proposal&proposal=9"))).toEqual({ ...NO_FILTERS, kind: "proposal", proposal: 9 });
    const bad = new URLSearchParams("kind=email&project=Bad%20Name&unread=yes&page=0&decision=-4");
    expect(readInboxFilters(bad)).toEqual(NO_FILTERS);
    expect(readInboxFilters(new URLSearchParams("decision=99999999999999999999")).decision).toBeNull();
  });

  it("writes them back in a stable order, leaving out what is not set", () => {
    expect(inboxSearch(NO_FILTERS)).toBe("");
    expect(inboxSearch({ ...NO_FILTERS, kind: "notice", project: "demo", unread: true, page: 2, decision: 7 })).toBe(
      "?decision=7&kind=notice&project=demo&unread=1&page=2",
    );
    expect(inboxSearch({ ...NO_FILTERS, q: "merged main", run: 12 })).toBe("?run=12&q=merged+main");
    expect(inboxSearch({ ...NO_FILTERS, proposal: 12 })).toBe("?proposal=12");
    const round = readInboxFilters(new URLSearchParams(inboxSearch({ ...NO_FILTERS, project: "ops", decision: 3 }).slice(1)));
    expect(round).toEqual({ ...NO_FILTERS, project: "ops", decision: 3 });
  });

  it("asks the API for the page the filters name; the open decision is not a filter", () => {
    expect(inboxQuery({ ...NO_FILTERS, kind: "decision", unread: true, q: "deploy", page: 3, decision: 9, proposal: 4 })).toEqual({
      kind: "decision",
      project: null,
      unread: true,
      q: "deploy",
      run: null,
      limit: 50,
      offset: 100,
    });
    expect(isFiltered({ ...NO_FILTERS, decision: 4, page: 2 })).toBe(false);
    expect(isFiltered({ ...NO_FILTERS, unread: true })).toBe(true);
    expect(isFiltered({ ...NO_FILTERS, q: "deploy" })).toBe(true);
    expect(isFiltered({ ...NO_FILTERS, run: 7 })).toBe(true);
  });
});

describe("the rest of the list by day and by run", () => {
  it("reads days in the page's time zone", () => {
    expect(dayKey("2026-10-23T18:30:00Z", "UTC")).toBe("2026-10-23");
    expect(dayKey("2026-10-23T18:30:00Z", "Asia/Ho_Chi_Minh")).toBe("2026-10-24");
    expect(daysBefore("2026-10-23", "2026-10-24")).toBe(1);
    expect(daysBefore("2025-12-31", "2026-01-01")).toBe(1);
    expect(daysBefore("2026-10-24", "2026-10-24")).toBe(0);
  });

  it("groups a run's notifications of one day where the newest stands, and leaves the rest alone", () => {
    const page = [
      note(9, { created_at: "2026-10-24T09:00:00Z" }),
      note(8, { run_id: 30, created_at: "2026-10-24T08:00:00Z", read_at: "2026-10-24T08:30:00Z" }),
      decision(7, "answered", { created_at: "2026-10-24T07:00:00Z" }),
      note(6, { run_id: null, created_at: "2026-10-24T06:00:00Z" }),
      note(5, { run_id: null, created_at: "2026-10-24T05:00:00Z" }),
      note(4, { project: "other", created_at: "2026-10-24T04:00:00Z" }),
      note(3, { created_at: "2026-10-23T20:00:00Z" }),
    ];
    const days = groupInbox(page, "UTC");
    expect(days.map((day) => [day.key, day.total, day.unread])).toEqual([
      ["2026-10-24", 6, 5],
      ["2026-10-23", 1, 1],
    ]);
    const [today, yesterday] = days;
    expect(today.entries.map((entry) => (entry.type === "run" ? `run ${entry.runId}: ${entry.notifications.map((item) => item.id)}` : `one ${entry.notification.id}`))).toEqual([
      "run 12: 9,7",
      "one 8",
      "one 6",
      "one 5",
      "one 4",
    ]);
    const run = today.entries[0];
    expect(run.type === "run" && run.unread).toBe(2);
    expect(yesterday.entries).toEqual([{ type: "single", notification: page[6] }]);
    // the same moment is the next day in Ho Chi Minh City: run 12's notice of 20:00 UTC joins the 24th
    expect(groupInbox(page, "Asia/Ho_Chi_Minh").map((day) => day.key)).toEqual(["2026-10-24"]);
  });

  it("opens by itself only an unread notice that asks something of its reader", () => {
    expect(opensByItself(note(1, { notice_kind: "canvas_baseline" }))).toBe(true);
    expect(opensByItself(note(1, { notice_kind: "curator_brief" }))).toBe(false);
    expect(opensByItself(note(1, { notice_kind: "merge_default_branch", details: { ask: "sample", repo: "api" } }))).toBe(true);
    expect(opensByItself(note(1, { notice_kind: "canvas_baseline", read_at: "2026-10-05T08:00:00Z" }))).toBe(false);
    expect(opensByItself(note(1))).toBe(false);
    expect(opensByItself(decision(1, "answered"))).toBe(false);
  });

  it("takes the first line with words of a body, without Markdown's marks", () => {
    expect(snippet("\n\n## **Run #12** pushed\nmore")).toBe("Run #12 pushed");
    expect(snippet("- `api` merged")).toBe("api merged");
    expect(snippet("1. first step")).toBe("first step");
    expect(snippet("  \n ")).toBeNull();
    expect(snippet(null)).toBeNull();
  });
});

describe("a page of notifications", () => {
  it("puts the decisions still open apart from notices and closed decisions, in the API's order", () => {
    const page = [decision(5, "open"), decision(3, "open"), note(9), decision(1, "answered"), note(2)];
    const { waiting, rest } = splitInbox(page);
    expect(waiting.map((item) => item.id)).toEqual([5, 3]);
    expect(rest.map((item) => item.id)).toEqual([9, 1, 2]);
  });

  it("puts the proposals still open beside the open decisions, and answered ones with the rest", () => {
    const proposal = (id: number, state: Notification["proposal_state"]) =>
      note(id, { kind: "proposal", notice_kind: null, proposal_id: 200 + id, proposal_state: state, link: `/inbox?proposal=${200 + id}` });
    const page = [decision(5, "open"), proposal(4, "open"), note(9), proposal(2, "accepted")];
    const { waiting, rest } = splitInbox(page);
    expect(waiting.map((item) => item.id)).toEqual([5, 4]);
    expect(rest.map((item) => item.id)).toEqual([9, 2]);
    expect(notificationOfProposal(page, 204)?.id).toBe(4);
    expect(notificationOfProposal(page, 105)).toBeNull();
  });

  it("finds a decision's notification and the unread ones, up to what one request marks", () => {
    const page = [decision(5, "open"), note(9, { read_at: "2026-10-05T08:00:00Z" }), note(2)];
    expect(notificationOfDecision(page, 105)?.id).toBe(5);
    expect(notificationOfDecision(page, 999)).toBeNull();
    expect(unreadIds(page)).toEqual([5, 2]);
    expect(unreadIds(page, 1)).toEqual([5]);
  });

  it("reads a notice's repo, branch and commits, and drops values of another shape", () => {
    const sha = "7c1e9a2f3b4c5d6e7f8091a2b3c4d5e6f7081920";
    expect(noticeFacts({ repo: "api", branch: "main", commits: [sha, 42, "not a sha"] })).toEqual({ repo: "api", branch: "main", commits: [sha] });
    expect(noticeFacts(null)).toEqual({ repo: null, branch: null, commits: [] });
    expect(noticeFacts({ repo: " ", commits: "abc1234" })).toEqual({ repo: null, branch: null, commits: [] });
  });

  it("caps the bell's number at 99+", () => {
    expect(bellNumber(0)).toBe("0");
    expect(bellNumber(99)).toBe("99");
    expect(bellNumber(100)).toBe("99+");
  });
});

describe("answers", () => {
  it("needs an option or some words, and words within 4 KiB of UTF-8", () => {
    expect(answerProblem(null, "   ")).toBe("empty");
    expect(answerProblem("deploy", "")).toBeNull();
    expect(answerProblem(null, "Wait for the backup")).toBeNull();
    expect(answerProblem(null, "ệ".repeat(MAX_ANSWER_BYTES / 3 + 1))).toBe("long");
    // The hub does what an option of its own decisions says: words alone are not an answer to one.
    expect(answerProblem(null, "close it", true)).toBe("option");
    expect(answerProblem("close", "", true)).toBeNull();
  });

  it("sends the option and the trimmed words, leaving out what is empty", () => {
    expect(answerBody("deploy", "  after 18:00 \n")).toEqual({ option: "deploy", text: "after 18:00" });
    expect(answerBody("deploy", "  ")).toEqual({ option: "deploy" });
    expect(answerBody(null, "no")).toEqual({ text: "no" });
  });

  it("lets only the run's owner answer an open decision whose run can still take it", () => {
    const open = { state: "open", owner: "octo", run_state: "waiting", category: "deploy" } as const;
    expect(answerAccess(open, "octo")).toBe("answer");
    expect(answerAccess({ ...open, run_state: "parked" }, "octo")).toBe("answer");
    expect(answerAccess({ ...open, run_state: "queued" }, "octo")).toBe("answer");
    // A local session whose turn ended takes the answer at its next turn; one that ended takes none.
    expect(answerAccess({ ...open, run_state: "idle" }, "octo")).toBe("answer");
    expect(answerAccess({ ...open, run_state: "ended" }, "octo")).toBe("runGone");
    expect(answerAccess(open, "mona")).toBe("notOwner");
    expect(answerAccess(open, null)).toBe("notOwner");
    expect(answerAccess({ ...open, run_state: "review" }, "octo")).toBe("runGone");
    expect(answerAccess({ ...open, state: "answered" }, "octo")).toBe("closed");
    expect(answerAccess({ ...open, state: "expired" }, "mona")).toBe("closed");
  });

  it("names the run a decision names by what it is", () => {
    expect(runLinkWords({ category: "triage", plan_id: null })).toBe("triageRunLink");
    expect(runLinkWords({ category: "release", plan_id: null })).toBe("releaseRunLink");
    expect(runLinkWords({ category: "scope", plan_id: null })).toBe("authorRunLink");
    expect(runLinkWords({ category: "deploy", plan_id: "lumen-sso" })).toBe("runLink");
  });

  it("lets the owner answer a decision of the hub's own whatever its run is", () => {
    // A stopped release cycle asks on its last run, done by then: the hub, not an agent, takes the answer.
    const stopped = { state: "open", owner: "octo", run_state: "done", category: "release" } as const;
    expect(answerAccess(stopped, "octo")).toBe("answer");
    expect(answerAccess({ ...stopped, category: "loop", run_state: "failed" }, "octo")).toBe("answer");
    expect(answerAccess(stopped, "mona")).toBe("notOwner");
    expect(answerAccess({ ...stopped, state: "cancelled" }, "octo")).toBe("closed");
    expect(answerAccess({ ...stopped, category: "deploy" }, "octo")).toBe("runGone");
  });

  it("names the option an answer chose", () => {
    const options = [
      { key: "deploy", label: "Deploy now", description: null, recommended: true },
      { key: "wait", label: "Wait", description: null, recommended: false },
    ];
    expect(chosenOption({ options, answer_option: "wait" })?.label).toBe("Wait");
    expect(chosenOption({ options, answer_option: null })).toBeNull();
  });
});

describe("the decision card", () => {
  const options = [
    { key: "deploy", label: "Deploy", description: null, recommended: true },
    { key: "wait", label: "Wait", description: null, recommended: false },
  ];

  it("picks the agent's pick for the owner, and nothing when it names no option offered", () => {
    expect(initialOption({ options, recommended: "deploy" })).toBe("deploy");
    expect(initialOption({ options, recommended: null })).toBe("deploy"); // the option's own flag
    expect(initialOption({ options: options.map((option) => ({ ...option, recommended: false })), recommended: null })).toBeNull();
    expect(initialOption({ options, recommended: "gone" })).toBeNull();
  });

  it("says when a waiting run parks, since when a parked one has, and nothing while its agent works", () => {
    const now = Date.parse("2026-10-05T08:00:00Z");
    const waiting = { state: "open", run_state: "waiting" } as const;
    expect(parkTiming(waiting, "2026-10-05T09:30:00Z", now)).toEqual({ kind: "parksIn", ms: 90 * 60_000 });
    expect(parkTiming(waiting, "2026-10-05T07:00:00Z", now)).toEqual({ kind: "parksIn", ms: 0 });
    expect(parkTiming(waiting, "2026-10-05T09:30:00Z", null)).toBeNull(); // before the page hydrates
    expect(parkTiming({ state: "open", run_state: "parked" }, "2026-10-05T07:00:00Z", now)).toEqual({ kind: "parked", at: "2026-10-05T07:00:00Z" });
    expect(parkTiming({ state: "open", run_state: "running" }, null, now)).toBeNull();
    expect(parkTiming(waiting, null, now)).toBeNull();
    expect(parkTiming({ state: "answered", run_state: "waiting" }, "2026-10-05T09:30:00Z", now)).toBeNull();
  });

  it("tells what a notice of the merge queue asks of an admin", () => {
    const notice = { notice_kind: "merge_default_branch" as const, project: "northwind" };
    expect(mergeAsk({ ...notice, details: { ask: "sample", sample_id: 4 } })).toBe("sample");
    expect(mergeAsk({ ...notice, details: { ask: "release" } })).toBe("release");
    expect(mergeAsk({ ...notice, details: { repo: "shop", branch: "main", commits: [] } })).toBeNull();
    expect(mergeAsk({ ...notice, project: null, details: { ask: "revert" } })).toBeNull();
    expect(mergeAsk({ notice_kind: "merge_paused", project: "northwind", details: { repo: "billing" } })).toBe("resume");
    expect(mergeAsk({ notice_kind: "push_default_branch", project: "northwind", details: { ask: "sample" } })).toBeNull();
  });
});
