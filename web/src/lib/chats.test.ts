import { describe, expect, it } from "vitest";

import { chatComposeHref } from "./chat-keys";
import {
  type ChatAgent,
  type ChatAuthor,
  chatFace,
  chatHref,
  chatName,
  filterChats,
  handedAgents,
  insertMention,
  isMine,
  knownPeople,
  listTime,
  mentionAt,
  mentionNames,
  mentionTargets,
  pullLinks,
  summaryFace,
  suggestTargets,
  titleProblem,
  unreadTotal,
} from "./chats";

function agent(handle: string, overrides: Partial<ChatAgent> = {}): ChatAgent {
  return {
    handle,
    project: "demo",
    name: handle[0].toUpperCase() + handle.slice(1),
    color: "#2563eb",
    icon_url: null,
    is_default: false,
    deleted: false,
    added_by: "octo",
    added_at: "2026-10-14T08:00:00Z",
    ...overrides,
  };
}

const person = (login: string) => ({ login, added_by: "octo", added_at: "2026-10-14T08:00:00Z", avatar_url: null });

const CHAT = {
  agents: [agent("reviewer", { name: "Code reviewer" }), agent("demo", { name: "Demo", is_default: true }), agent("gone", { deleted: true })],
  people: [person("octo"), person("mai")],
};

describe("mentions as the hub reads them", () => {
  it("reads each name once, lowered, in order, not inside code nor after a letter, dot or slash", () => {
    const text = "@Reviewer and @demo, mail me at a@b.org, see docs/@x\n`@code` and\n```\n@fenced\n```\nthen @reviewer again (@mai)";
    expect(mentionNames(text)).toEqual(["reviewer", "demo", "mai"]);
  });

  it("finds the name being typed at the caret, and puts the picked one in with a space after it", () => {
    expect(mentionAt("hi @rev", 7)).toEqual({ start: 3, end: 7, query: "rev" });
    expect(mentionAt("hi @", 4)).toEqual({ start: 3, end: 4, query: "" });
    expect(mentionAt("mail a@rev", 10)).toBeNull();
    expect(mentionAt("hi @rev there", 13)).toBeNull();
    const at = mentionAt("hi @rev please", 7);
    expect(at).not.toBeNull();
    expect(insertMention("hi @rev please", at!, "reviewer")).toEqual({ text: "hi @reviewer please", caret: 13 });
    expect(insertMention("hi @rev", mentionAt("hi @rev", 7)!, "reviewer")).toEqual({ text: "hi @reviewer ", caret: 13 });
  });

  it("offers the chat's agents, then its projects, then its people other than the visitor; never a deleted agent", () => {
    const targets = mentionTargets(CHAT, "OCTO");
    expect(targets.map((target) => [target.kind, target.name])).toEqual([
      ["agent", "reviewer"],
      ["project", "demo"],
      ["person", "mai"],
    ]);
    expect(suggestTargets(targets, "").map((target) => target.name)).toEqual(["reviewer", "demo", "mai"]);
    expect(suggestTargets(targets, "co").map((target) => target.name)).toEqual(["reviewer"]);
    expect(suggestTargets(targets, "m").map((target) => target.name)).toEqual(["mai", "demo"]);
    expect(suggestTargets(targets, "zzz")).toEqual([]);
  });

  it("says which agents a message hands work to: those of the chat it names, deleted ones left out", () => {
    expect(handedAgents("@reviewer and @demo, @gone and @mai", CHAT).map((item) => item.handle)).toEqual(["reviewer", "demo"]);
    expect(handedAgents("no mention", CHAT)).toEqual([]);
  });
});

describe("chat helpers", () => {
  it("links a chat, at one message when given, or with its composer focused", () => {
    expect(chatHref(12)).toBe("/chats/12");
    expect(chatHref(12, 40)).toBe("/chats/12?message=40");
    expect(chatComposeHref(12)).toBe("/chats/12?compose=1");
  });

  it("finds the GitHub pull requests a message links, each once", () => {
    const body = "Draft PR: https://github.com/example-org/api/pull/7 and again https://github.com/example-org/api/pull/7, docs https://github.com/example-org/docs/pull/12.";
    expect(pullLinks(body)).toEqual([
      { url: "https://github.com/example-org/api/pull/7", repo: "example-org/api", number: 7 },
      { url: "https://github.com/example-org/docs/pull/12", repo: "example-org/docs", number: 12 },
    ]);
    expect(pullLinks("https://gitlab.com/a/b/pull/1")).toEqual([]);
  });

  it("adds the unread messages, filters chats by title or member, and suggests the people of other chats", () => {
    const chats = [
      { title: "Release", unread: 2, people: ["octo", "Mai"], agents: ["reviewer"] },
      { title: "Docs", unread: 0, people: ["octo", "lan"], agents: [] },
    ];
    expect(unreadTotal(chats)).toBe(2);
    expect(filterChats(chats, "rel").map((chat) => chat.title)).toEqual(["Release"]);
    expect(filterChats(chats, "@review").map((chat) => chat.title)).toEqual(["Release"]);
    expect(filterChats(chats, "LAN").map((chat) => chat.title)).toEqual(["Docs"]);
    expect(filterChats(chats, " ")).toHaveLength(2);
    expect(knownPeople(chats, "octo", ["mai"])).toEqual(["lan"]);
  });

  it("checks a title as the hub does: one line of 1 to 120 characters", () => {
    expect(titleProblem("Release talk")).toBeNull();
    expect(titleProblem("  ")).toBe("blank");
    expect(titleProblem("x".repeat(121))).toBe("long");
    expect(titleProblem("a\nb")).toBe("line");
  });
});

const REVIEWER: ChatAuthor = { kind: "agent", handle: "reviewer", project: "demo", name: "Code reviewer", color: "#2563eb", icon_url: null, deleted: false };
const MAI: ChatAuthor = { kind: "person", login: "mai", name: "mai", deleted: false, avatar_url: "https://avatars.githubusercontent.com/u/7?s=96&v=4" };
const LAN: ChatAuthor = { kind: "person", login: "lan", name: "lan", deleted: false, avatar_url: null };

describe("a chat's face and name", () => {
  const summary = { title: "Release", kind: "group" as const, image_url: null, direct_agent: null, faces: [] as ChatAuthor[] };

  it("draws a direct chat with its agent and calls it by the agent's name now", () => {
    const direct = { ...summary, kind: "direct" as const, title: "Old name", direct_agent: REVIEWER, faces: [REVIEWER] };
    expect(summaryFace(direct)).toEqual({ kind: "agent", agent: REVIEWER });
    expect(chatName(direct)).toBe("Code reviewer");
    expect(chatName(summary)).toBe("Release");
  });

  it("draws a group with its image, else two of its other members, else the one, else none", () => {
    expect(summaryFace({ ...summary, image_url: "/v1/chats/4/image?v=ab", faces: [MAI, REVIEWER, LAN] })).toEqual({ kind: "image", url: "/v1/chats/4/image?v=ab", faces: [MAI, REVIEWER] });
    expect(summaryFace({ ...summary, faces: [MAI, REVIEWER, LAN] })).toEqual({ kind: "members", faces: [MAI, REVIEWER] });
    expect(summaryFace({ ...summary, faces: [LAN] })).toEqual({ kind: "members", faces: [LAN] });
    expect(summaryFace(summary)).toEqual({ kind: "none" });
  });

  it("draws a chat's head from the chat: its other members in the order they were added", () => {
    const people = [
      { login: "octo", added_by: "octo", added_at: "2026-10-14T08:00:00Z", avatar_url: null },
      { login: "lan", added_by: "octo", added_at: "2026-10-14T08:03:00Z", avatar_url: null },
      { login: "mai", added_by: "octo", added_at: "2026-10-14T08:01:00Z", avatar_url: MAI.avatar_url ?? null },
    ];
    const agents = [agent("reviewer", { name: "Code reviewer", added_at: "2026-10-14T08:02:00Z" })];
    const group = { kind: "group" as const, image_url: null, agents, people };
    expect(chatFace(group, "OCTO")).toEqual({ kind: "members", faces: [MAI, { ...REVIEWER, project: "demo" }] });
    expect(chatFace({ ...group, image_url: "/v1/chats/4/image?v=cd" }, "octo")).toMatchObject({ kind: "image", url: "/v1/chats/4/image?v=cd" });
    expect(chatFace({ kind: "direct", image_url: null, agents, people: people.slice(0, 1) }, "octo")).toEqual({ kind: "agent", agent: REVIEWER });
    expect(chatFace({ ...group, agents: [], people: people.slice(0, 1) }, "octo")).toEqual({ kind: "none" });
  });

  it("finds a direct chat by its agent's name, and tells the visitor's messages by login, any case", () => {
    const chats = [
      { ...summary, title: "Old name", kind: "direct" as const, direct_agent: REVIEWER, people: ["octo"], agents: ["reviewer"] },
      { ...summary, title: "Docs", people: ["octo"], agents: [] },
    ];
    expect(filterChats(chats, "code rev").map((chat) => chat.title)).toEqual(["Old name"]);
    expect(isMine({ kind: "person", login: "Octo" }, "octo")).toBe(true);
    expect(isMine({ kind: "agent", login: null }, "octo")).toBe(false);
    expect(isMine({ kind: "person", login: "octo" }, null)).toBe(false);
  });
});

describe("when a row says its last message came", () => {
  const zone = "Asia/Ho_Chi_Minh"; // UTC+7
  const now = new Date("2026-10-20T03:00:00Z"); // 10:00 on Tuesday 20 October there

  it("says the time today, the weekday within the week, the date this year, and the year before it", () => {
    expect(listTime(new Date("2026-10-19T17:30:00Z"), now, zone)).toBe("time"); // 00:30 the same day there
    expect(listTime(new Date("2026-10-19T16:30:00Z"), now, zone)).toBe("weekday"); // 23:30 the day before
    expect(listTime(new Date("2026-10-14T03:00:00Z"), now, zone)).toBe("weekday"); // six days before
    expect(listTime(new Date("2026-10-13T03:00:00Z"), now, zone)).toBe("date"); // a week before
    expect(listTime(new Date("2025-12-31T03:00:00Z"), now, zone)).toBe("year");
    // A clock a little ahead of the visitor's: a message "after" now is today's or a date, never a weekday.
    expect(listTime(new Date("2026-10-21T03:00:00Z"), now, zone)).toBe("date");
  });

  it("counts the days of the page's time zone, not the browser's", () => {
    const late = new Date("2026-10-19T23:30:00Z"); // 06:30 on the 20th there, 23:30 on the 19th in UTC
    expect(listTime(late, now, zone)).toBe("time");
    expect(listTime(late, now, "UTC")).toBe("weekday");
  });
});
