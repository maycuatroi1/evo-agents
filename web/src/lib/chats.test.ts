import { describe, expect, it } from "vitest";

import {
  type ChatAgent,
  chatHref,
  filterChats,
  handedAgents,
  insertMention,
  knownPeople,
  mentionAt,
  mentionNames,
  mentionTargets,
  pullLinks,
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
  it("links a chat, at one message when given", () => {
    expect(chatHref(12)).toBe("/chats/12");
    expect(chatHref(12, 40)).toBe("/chats/12?message=40");
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
