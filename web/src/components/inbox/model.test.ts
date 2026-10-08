import { describe, expect, it } from "vitest";

import {
  answerAccess,
  answerBody,
  answerProblem,
  bellNumber,
  chosenOption,
  inboxQuery,
  initialOption,
  inboxSearch,
  isFiltered,
  noticeFacts,
  NO_FILTERS,
  notificationOfDecision,
  parkTiming,
  readInboxFilters,
  notificationOfProposal,
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
    expect(readInboxFilters(params)).toEqual({ kind: "decision", project: "demo", unread: true, page: 3, decision: 42, proposal: null });
    expect(readInboxFilters(new URLSearchParams("kind=proposal&proposal=9"))).toEqual({ ...NO_FILTERS, kind: "proposal", proposal: 9 });
    const bad = new URLSearchParams("kind=email&project=Bad%20Name&unread=yes&page=0&decision=-4");
    expect(readInboxFilters(bad)).toEqual(NO_FILTERS);
    expect(readInboxFilters(new URLSearchParams("decision=99999999999999999999")).decision).toBeNull();
  });

  it("writes them back in a stable order, leaving out what is not set", () => {
    expect(inboxSearch(NO_FILTERS)).toBe("");
    expect(inboxSearch({ kind: "notice", project: "demo", unread: true, page: 2, decision: 7, proposal: null })).toBe(
      "?decision=7&kind=notice&project=demo&unread=1&page=2",
    );
    expect(inboxSearch({ ...NO_FILTERS, proposal: 12 })).toBe("?proposal=12");
    const round = readInboxFilters(new URLSearchParams(inboxSearch({ ...NO_FILTERS, project: "ops", decision: 3 }).slice(1)));
    expect(round).toEqual({ ...NO_FILTERS, project: "ops", decision: 3 });
  });

  it("asks the API for the page the filters name; the open decision is not a filter", () => {
    expect(inboxQuery({ kind: "decision", project: null, unread: true, page: 3, decision: 9, proposal: 4 })).toEqual({
      kind: "decision",
      project: null,
      unread: true,
      limit: 50,
      offset: 100,
    });
    expect(isFiltered({ ...NO_FILTERS, decision: 4, page: 2 })).toBe(false);
    expect(isFiltered({ ...NO_FILTERS, unread: true })).toBe(true);
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
  });

  it("sends the option and the trimmed words, leaving out what is empty", () => {
    expect(answerBody("deploy", "  after 18:00 \n")).toEqual({ option: "deploy", text: "after 18:00" });
    expect(answerBody("deploy", "  ")).toEqual({ option: "deploy" });
    expect(answerBody(null, "no")).toEqual({ text: "no" });
  });

  it("lets only the run's owner answer an open decision whose run can still take it", () => {
    const open = { state: "open", owner: "octo", run_state: "waiting" } as const;
    expect(answerAccess(open, "octo")).toBe("answer");
    expect(answerAccess({ ...open, run_state: "parked" }, "octo")).toBe("answer");
    expect(answerAccess({ ...open, run_state: "queued" }, "octo")).toBe("answer");
    expect(answerAccess(open, "mona")).toBe("notOwner");
    expect(answerAccess(open, null)).toBe("notOwner");
    expect(answerAccess({ ...open, run_state: "review" }, "octo")).toBe("runGone");
    expect(answerAccess({ ...open, state: "answered" }, "octo")).toBe("closed");
    expect(answerAccess({ ...open, state: "expired" }, "mona")).toBe("closed");
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
});
