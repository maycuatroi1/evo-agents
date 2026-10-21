import type { Sprint, Task, TaskDetail, TasksSettings } from "@/components/tasks/api";

/** Tasks, sprints and the default config for the Tasks pages' unit tests: the shape of the plugin's answers. */
export const SETTINGS: TasksSettings = {
  prefix: "T",
  statuses: [
    { key: "backlog", name: "Backlog", group: "todo" },
    { key: "todo", name: "To do", group: "todo" },
    { key: "in_progress", name: "In progress", group: "doing" },
    { key: "in_review", name: "In review", group: "review" },
    { key: "done", name: "Done", group: "done" },
    { key: "cancelled", name: "Cancelled", group: "cancelled" },
  ],
  types: ["task", "bug", "story"],
  on_steps_done: "in_review",
  srs: true,
};

export function task(number: number, patch: Partial<Task> = {}): Task {
  return {
    number,
    ref: `T-${number}`,
    type: "task",
    title: `Task ${number}`,
    status: "todo",
    status_group: "todo",
    priority: "medium",
    due_on: null,
    sprint: "Sprint 7",
    parent: null,
    assignees: [],
    roles: { ba: [], qa: [], dev: [] },
    steps_done: 0,
    steps_total: 0,
    codes: [],
    label: { level: "internal" },
    created_by: "alice",
    created_at: "2026-10-13T02:00:00Z",
    updated_at: "2026-10-13T02:00:00Z",
    ...patch,
  };
}

export function sprint(name: string, patch: Partial<Sprint> = {}): Sprint {
  return {
    name,
    goal: "",
    starts_on: null,
    ends_on: null,
    state: "planned",
    created_by: "alice",
    created_at: "2026-10-01T02:00:00Z",
    closed_at: null,
    tasks: 0,
    ...patch,
  };
}

export const SPRINTS: Sprint[] = [
  sprint("Sprint 8", { starts_on: "2026-10-27", ends_on: "2026-11-09" }),
  sprint("Sprint 7", { state: "active", starts_on: "2026-10-13", ends_on: "2026-10-26", goal: "Cancel before packing" }),
  sprint("Sprint 6", { state: "closed", starts_on: "2026-09-29", ends_on: "2026-10-12", closed_at: "2026-10-12T10:00:00Z" }),
];

export const TASKS: Task[] = [
  task(11, { title: "Hold stock at checkout", status: "done", status_group: "done", assignees: ["chi"], codes: ["UC ORD-1"], steps_done: 1, steps_total: 1 }),
  task(12, { title: "Payment retries", status: "in_progress", status_group: "doing", assignees: ["chi"], roles: { ba: ["alice"], qa: ["bao"], dev: ["chi"] }, codes: ["UC ORD-2", "BR ORD2-1"] }),
  task(14, { title: "Cancel API follows packing", status: "in_review", status_group: "review", priority: "urgent", assignees: ["chi", "bao"], codes: ["UC ORD-3"], due_on: "2026-10-21" }),
  task(15, { title: "Test cases for the cancel window", roles: { ba: [], qa: ["bao"], dev: [] }, codes: ["UC ORD-3"] }),
  task(16, { title: "Dropped idea", status: "cancelled", status_group: "cancelled" }),
];

export function detailOf(base: Task, patch: Partial<TaskDetail> = {}): TaskDetail {
  return { ...base, description_md: "", external_ref: null, links: [], events: [], comments: [], ...patch };
}
