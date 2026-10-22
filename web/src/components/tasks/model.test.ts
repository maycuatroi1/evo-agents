import { describe, expect, it } from "vitest";

import { newTaskHref, srsItemHref, taskHref, ticketRef } from "@/lib/core-plugin-links";
import { SETTINGS, sprint, SPRINTS, task, TASKS } from "@/test/fixtures/tasks";

import { boardHref } from "./links";
import {
  boardColumns,
  boardList,
  defaultSprint,
  eventWords,
  filterTasks,
  holds,
  isFinished,
  linkCount,
  linkedCodes,
  NO_FILTER,
  orderedSprints,
  parseLinks,
  partsOf,
  patchTask,
  pullName,
  sprintChoices,
} from "./model";

describe("sprints", () => {
  it("orders the active sprint first, the planned ones by start, the closed newest first", () => {
    const later = sprint("Sprint 9", { starts_on: "2026-11-10" });
    const undated = sprint("Someday");
    const older = sprint("Sprint 5", { state: "closed", closed_at: "2026-09-28T10:00:00Z" });
    expect(orderedSprints([...SPRINTS, later, undated, older]).map((one) => one.name)).toEqual([
      "Sprint 7",
      "Sprint 8",
      "Sprint 9",
      "Someday",
      "Sprint 6",
      "Sprint 5",
    ]);
  });

  it("opens the board on the active sprint, else the next planned one, else none", () => {
    expect(defaultSprint(SPRINTS)?.name).toBe("Sprint 7");
    expect(defaultSprint(SPRINTS.filter((one) => one.state !== "active"))?.name).toBe("Sprint 8");
    expect(defaultSprint(SPRINTS.filter((one) => one.state === "closed"))).toBeNull();
    expect(defaultSprint([])).toBeNull();
  });

  it("reads what the board's URL asks for, and waits for the sprints only when it asks for neither", () => {
    expect(boardList({ sprint: "Sprint 6" }, undefined)).toEqual({ kind: "sprint", name: "Sprint 6" });
    expect(boardList({ backlog: true }, undefined)).toEqual({ kind: "backlog" });
    expect(boardList(null, undefined)).toBeNull();
    expect(boardList(null, SPRINTS)).toEqual({ kind: "sprint", name: "Sprint 7" });
    expect(boardList(null, [])).toEqual({ kind: "backlog" });
  });

  it("offers every sprint not closed, and keeps a closed one a task is in", () => {
    expect(sprintChoices(SPRINTS, null).map((one) => one.name)).toEqual(["Sprint 7", "Sprint 8"]);
    expect(sprintChoices(SPRINTS, "Sprint 6").map((one) => one.name)).toEqual(["Sprint 7", "Sprint 8", "Sprint 6"]);
  });
});

describe("the board", () => {
  it("has a column per status but the cancelled ones, which it counts apart", () => {
    const { columns, cancelled } = boardColumns(SETTINGS.statuses, TASKS);
    expect(columns.map((column) => [column.key, column.tasks.map((one) => one.ref)])).toEqual([
      ["backlog", []],
      ["todo", ["T-15"]],
      ["in_progress", ["T-12"]],
      ["in_review", ["T-14"]],
      ["done", ["T-11"]],
    ]);
    expect(cancelled).toBe(1);
  });

  it("puts a status the config no longer lists in a column of its own, last", () => {
    const stale = task(20, { status: "blocked", status_group: null });
    const { columns } = boardColumns(SETTINGS.statuses, [...TASKS, stale]);
    expect(columns.at(-1)).toMatchObject({ key: null, group: null, tasks: [stale] });
  });

  it("filters by a role held, a linked code and the viewer's own slots", () => {
    expect(filterTasks(TASKS, NO_FILTER)).toHaveLength(TASKS.length);
    expect(filterTasks(TASKS, { ...NO_FILTER, role: "qa" }).map((one) => one.ref)).toEqual(["T-12", "T-15"]);
    expect(filterTasks(TASKS, { ...NO_FILTER, code: "UC ORD-3" }).map((one) => one.ref)).toEqual(["T-14", "T-15"]);
    expect(filterTasks(TASKS, { ...NO_FILTER, mine: "bao" }).map((one) => one.ref)).toEqual(["T-12", "T-14", "T-15"]);
    expect(linkedCodes(TASKS)).toEqual(["BR ORD2-1", "UC ORD-1", "UC ORD-2", "UC ORD-3"]);
  });

  it("says whose part a task is and whether anyone still works on it", () => {
    const [done, retries] = TASKS;
    expect(holds(retries, "alice") && !holds(done, "alice")).toBe(true);
    expect(partsOf(retries, "chi")).toEqual(["assignee", "dev"]);
    expect(isFinished(done) && !isFinished(retries) && isFinished(TASKS[4])).toBe(true);
    expect(isFinished(task(1, { status_group: null }))).toBe(false);
  });

  it("moves a task before the hub answers: its status with the status's group, its people and fields", () => {
    const moved = patchTask(TASKS[3], { status: "in_review", assignees: ["bao"], roles: { qa: ["eve"] }, sprint: null }, SETTINGS);
    expect(moved).toMatchObject({ status: "in_review", status_group: "review", assignees: ["bao"], sprint: null });
    expect(moved.roles).toEqual({ ba: [], qa: ["eve"], dev: [] });
    expect(patchTask(TASKS[3], { status: "gone" }, SETTINGS).status_group).toBeNull();
    expect(TASKS[3].status).toBe("todo");
  });
});

describe("links typed by a writer", () => {
  it("reads codes, plan steps, runs and pull requests parted by commas or lines", () => {
    const { links, bad } = parseLinks("UC ORD-3,  BR  ORD3-2\norder-cancel#2, #431, run 12, https://github.com/acme/shop/pull/88");
    expect(links).toEqual({
      use_cases: ["UC ORD-3", "BR ORD3-2"],
      steps: ["order-cancel#2"],
      runs: [431, 12],
      pulls: ["https://github.com/acme/shop/pull/88"],
    });
    expect(bad).toEqual([]);
    expect(linkCount(links)).toBe(6);
  });

  it("refuses what no link can be", () => {
    expect(parseLinks("a#b#c, #x, , ").bad).toEqual(["a#b#c", "#x"]);
    expect(parseLinks("x".repeat(101)).bad).toHaveLength(1);
    expect(linkCount(parseLinks(" , ").links)).toBe(0);
  });

  it("names a pull or merge request as people do", () => {
    expect(pullName("https://github.com/acme/shop/pull/88")).toBe("acme/shop#88");
    expect(pullName("https://gitlab.example.org/group/sub/shop/-/merge_requests/5")).toBe("group/sub/shop!5");
    expect(pullName("https://git.example.org/evo/shop/pulls/3/")).toBe("git.example.org/evo/shop/pulls/3");
    expect(pullName("not a url")).toBe("not a url");
  });
});

describe("a task's history", () => {
  const event = (field: string, from_value: string | null, to_value: string | null) => ({ field, from_value, to_value });

  it("reads each kind of row the hub writes", () => {
    expect(eventWords(event("created", null, "T-1"))).toEqual({ kind: "created" });
    expect(eventWords(event("status", "todo", "done"))).toEqual({ kind: "status", from: "todo", to: "done" });
    expect(eventWords(event("status", null, "done"))).toEqual({ kind: "status", from: null, to: "done" });
    expect(eventWords(event("description", null, null))).toEqual({ kind: "description" });
    expect(eventWords(event("outcome", null, null))).toEqual({ kind: "outcome" });
    expect(eventWords(event("priority", "low", "high"))).toEqual({ kind: "set", field: "priority", from: "low", to: "high" });
    expect(eventWords(event("sprint", "Sprint 7", null))).toEqual({ kind: "cleared", field: "sprint", from: "Sprint 7" });
    expect(eventWords(event("qa", "bao", "eve"))).toEqual({ kind: "people", field: "qa", to: "eve" });
    expect(eventWords(event("link", null, "srs_item UC ORD-1"))).toEqual({ kind: "linked", link: "srs_item", target: "UC ORD-1" });
    expect(eventWords(event("link", null, "run 12"))).toEqual({ kind: "linked", link: "run", target: "#12" });
    expect(eventWords(event("link", "plan_step link 3", null))).toEqual({ kind: "unlinked", link: "plan_step" });
    expect(eventWords(event("plan_step", "in_progress", "done"))).toEqual({ kind: "step", to: "done" });
    expect(eventWords(event("label", "a", "b"))).toEqual({ kind: "other", field: "label", from: "a", to: "b" });
  });
});

describe("where the pages are", () => {
  it("links the board, a task, a new task and an SRS item, and reads a Ticket node's task", () => {
    expect(boardHref("demo")).toBe("/p/demo/x/tasks/board");
    expect(boardHref("demo", { sprint: "Sprint 7" })).toBe("/p/demo/x/tasks/board?sprint=Sprint+7");
    expect(boardHref("demo", { backlog: true })).toBe("/p/demo/x/tasks/board?backlog=1");
    expect(taskHref("demo", "T-14")).toBe("/p/demo/x/tasks/board/T-14");
    expect(newTaskHref("demo", { link: "UC ORD-3" })).toBe("/p/demo/x/tasks/board/new?link=UC+ORD-3");
    expect(newTaskHref("demo")).toBe("/p/demo/x/tasks/board/new");
    expect(srsItemHref("demo", "demo-shop", "UC ORD-3")).toBe("/p/demo/x/srs/documents/demo-shop/items/UC%20ORD-3");
    expect(ticketRef("plugin.tasks:ticket:SHOP-42")).toBe("SHOP-42");
    expect(ticketRef("usecase:UC ORD-1")).toBeNull();
  });
});
