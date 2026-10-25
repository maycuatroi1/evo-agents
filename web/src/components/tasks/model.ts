import type { Role, Sprint, Task, TaskEdit, TaskEvent, TaskGroup, TaskList, TasksSettings, TaskStatus } from "./api";

/**
 * The pure model of the Tasks pages: which sprint a board opens on, its columns and filters, whose part a task is, how
 * a writer's text of links reads (as the hub takes them, evo_agents.hub.server.tasks.links), what a change does to a
 * task before the hub answers, and how a row of a task's history reads. Nothing here reads the API.
 */

export type Viewer = { login: string; role: "reader" | "writer" | "admin" | null };

export const MAX_TITLE = 300; // evo_agents.hub.tables_tasks.MAX_TITLE_CHARS
export const MAX_DESCRIPTION = 100_000; // MAX_DESCRIPTION_CHARS

export function canWrite(viewer: Viewer | null): boolean {
  return viewer?.role === "writer" || viewer?.role === "admin";
}

/** The groups of a task nobody works on any more (evo_agents.hub.server.tasks.FINISHED). */
const FINISHED: ReadonlySet<TaskGroup> = new Set(["done", "cancelled"]);

export function isFinished(task: Pick<Task, "status_group">): boolean {
  return task.status_group !== null && FINISHED.has(task.status_group);
}

const SPRINT_ORDER: Record<Sprint["state"], number> = { active: 0, planned: 1, closed: 2 };

function startOf(sprint: Sprint): string {
  return sprint.starts_on ?? "9999-12-31";
}

/** Sprints as the picker and the backlog list them: the active one, the planned ones by start, the closed newest first. */
export function orderedSprints(sprints: readonly Sprint[]): Sprint[] {
  return [...sprints].sort((a, b) => {
    const state = SPRINT_ORDER[a.state] - SPRINT_ORDER[b.state];
    if (state !== 0) return state;
    if (a.state === "closed") return (b.closed_at ?? "").localeCompare(a.closed_at ?? "");
    return startOf(a).localeCompare(startOf(b)) || a.name.localeCompare(b.name);
  });
}

/** The sprint a board opens on: the active one, else the planned one that starts first; null when there is none. */
export function defaultSprint(sprints: readonly Sprint[]): Sprint | null {
  const [first] = orderedSprints(sprints);
  return first && first.state !== "closed" ? first : null;
}

/** What a board's URL asks for: a sprint by name (`?sprint=`), the tasks of no sprint (`?backlog=1`), or neither. */
export type BoardAsk = { sprint: string } | { backlog: true } | null;

/**
 * The tasks a board shows: the sprint asked for, the tasks of no sprint, else the default sprint's (`defaultSprint`)
 * or, in a project without one, the tasks of no sprint. Null while the sprints it would choose from are not known.
 */
export function boardList(ask: BoardAsk, sprints: readonly Sprint[] | undefined): TaskList | null {
  if (ask && "sprint" in ask) return { kind: "sprint", name: ask.sprint };
  if (ask) return { kind: "backlog" };
  if (!sprints) return null;
  const sprint = defaultSprint(sprints);
  return sprint ? { kind: "sprint", name: sprint.name } : { kind: "backlog" };
}

/** The sprints a task may move to: every sprint not closed, and the one it is in even when closed. */
export function sprintChoices(sprints: readonly Sprint[], current: string | null): Sprint[] {
  return orderedSprints(sprints).filter((sprint) => sprint.state !== "closed" || sprint.name === current);
}

export function statusOf(settings: Pick<TasksSettings, "statuses">, key: string): TaskStatus | null {
  return settings.statuses.find((status) => status.key === key) ?? null;
}

/** The kinds of SRS item a task links to (evo_agents.hub.server.srs CODED kinds), whose names the pages know. */
export const ITEM_KINDS = ["use_case", "business_rule", "screen", "nfr", "cbr"] as const;
export type ItemKind = (typeof ITEM_KINDS)[number];

export function isItemKind(kind: string): kind is ItemKind {
  return (ITEM_KINDS as readonly string[]).includes(kind);
}

/** A column of the board: a status of the project, or (key null) the statuses its config no longer lists. */
export type BoardColumn = { key: string | null; name: string; group: TaskGroup | null; tasks: Task[] };

/**
 * The board's columns: one per status in the config's order, except the cancelled ones, then the tasks whose status
 * the config no longer lists, when there are some. A cancelled task is off the board; `cancelled` counts them.
 */
export function boardColumns(statuses: readonly TaskStatus[], tasks: readonly Task[]): { columns: BoardColumn[]; cancelled: number } {
  const shown = statuses.filter((status) => status.group !== "cancelled");
  const columns: BoardColumn[] = shown.map((status) => ({
    key: status.key,
    name: status.name,
    group: status.group,
    tasks: tasks.filter((task) => task.status === status.key),
  }));
  const known = new Set(statuses.map((status) => status.key));
  const other = tasks.filter((task) => !known.has(task.status));
  if (other.length) columns.push({ key: null, name: "", group: null, tasks: other });
  const cancelled = new Set(statuses.filter((status) => status.group === "cancelled").map((status) => status.key));
  return { columns, cancelled: tasks.filter((task) => cancelled.has(task.status)).length };
}

/** Whether `login` stands in a slot of the task: an assignee, its BA, QA or Dev. */
export function holds(task: Pick<Task, "assignees" | "roles">, login: string): boolean {
  return task.assignees.includes(login) || Object.values(task.roles).some((logins) => logins.includes(login));
}

/** The slots `login` stands in on the task, the assignee's first. */
export function partsOf(task: Pick<Task, "assignees" | "roles">, login: string): ("assignee" | Role)[] {
  const roles = (["ba", "qa", "dev"] as const).filter((role) => task.roles[role].includes(login));
  return [...(task.assignees.includes(login) ? (["assignee"] as const) : []), ...roles];
}

export type BoardFilter = { role: Role | null; code: string | null; mine: string | null };

export const NO_FILTER: BoardFilter = { role: null, code: null, mine: null };

export function filtering(filter: BoardFilter): boolean {
  return filter.role !== null || filter.code !== null || filter.mine !== null;
}

/** The tasks a board's filters keep: someone in the role, a link to the code, the viewer in a slot. */
export function filterTasks(tasks: readonly Task[], filter: BoardFilter): Task[] {
  return tasks.filter(
    (task) =>
      (filter.role === null || task.roles[filter.role].length > 0) &&
      (filter.code === null || task.codes.includes(filter.code)) &&
      (filter.mine === null || holds(task, filter.mine)),
  );
}

/** Every code the tasks link to, once each, in order. */
export function linkedCodes(tasks: readonly Task[]): string[] {
  return [...new Set(tasks.flatMap((task) => task.codes))].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
}

/** Links to add, as the API takes them. */
export type LinksIn = { use_cases: string[]; steps: string[]; runs: number[]; pulls: string[] };

const MAX_REF = 100; // a code or a plan#step (evo_agents.hub.tables_tasks.MAX_STEP_CHARS)

/**
 * What a writer typed as links, one or more parted by commas or lines: a URL is a pull or merge request, `#431` or
 * `run 431` a run, `plan#step` a plan step, anything else the code of an SRS item (`UC ORD-3`, which holds a space).
 * `bad` lists what cannot be one (too long, or a `#` the other forms do not take).
 */
export function parseLinks(text: string): { links: LinksIn; bad: string[] } {
  const links: LinksIn = { use_cases: [], steps: [], runs: [], pulls: [] };
  const bad: string[] = [];
  for (const raw of text.split(/[,\n]/)) {
    const token = raw.trim().replace(/\s+/g, " ");
    if (!token) continue;
    const run = /^(?:run\s*)?#(\d+)$/i.exec(token) ?? /^run\s+(\d+)$/i.exec(token);
    if (/^https?:\/\/\S+$/i.test(token)) links.pulls.push(token);
    else if (run) links.runs.push(Number(run[1]));
    else if (token.length > MAX_REF) bad.push(token);
    else if (/^[^#\s]+#[^#\s]+$/.test(token)) links.steps.push(token);
    else if (token.includes("#")) bad.push(token);
    else links.use_cases.push(token);
  }
  return { links, bad };
}

export function linkCount(links: LinksIn): number {
  return links.use_cases.length + links.steps.length + links.runs.length + links.pulls.length;
}

/** The task as it will be once the hub takes `change`, for the board to move it at once. */
export function patchTask(task: Task, change: TaskEdit, settings: Pick<TasksSettings, "statuses">): Task {
  const next: Task = { ...task };
  if (change.status) {
    next.status = change.status;
    next.status_group = statusOf(settings, change.status)?.group ?? null;
  }
  if (change.assignees) next.assignees = [...change.assignees];
  if (change.priority) next.priority = change.priority;
  if (change.type) next.type = change.type;
  if (change.title) next.title = change.title;
  if (change.sprint !== undefined) next.sprint = change.sprint;
  if (change.due_on !== undefined) next.due_on = change.due_on;
  for (const role of ["ba", "qa", "dev"] as const) {
    const given = change.roles?.[role];
    if (given) next.roles = { ...next.roles, [role]: [...given] };
  }
  return next;
}

/** A pull or merge request as people name it: `owner/repo#88`, `group/repo!5`; the address without its scheme else. */
export function pullName(url: string): string {
  try {
    const { host, pathname } = new URL(url);
    const github = /^\/([^/]+\/[^/]+)\/pull\/(\d+)/.exec(pathname);
    if (github) return `${github[1]}#${github[2]}`;
    const gitlab = /^\/(.+?)\/-\/merge_requests\/(\d+)/.exec(pathname);
    if (gitlab) return `${gitlab[1]}!${gitlab[2]}`;
    return `${host}${pathname}`.replace(/\/$/, "");
  } catch {
    return url;
  }
}

/** The fields whose change a task's history names by field: everything but its status, people, links and steps. */
export const NAMED_FIELDS = ["title", "type", "priority", "due_on", "sprint", "parent"] as const;
export type NamedField = (typeof NAMED_FIELDS)[number];
const PEOPLE_FIELDS = ["assignee", "ba", "qa", "dev"] as const;
export type PeopleField = (typeof PEOPLE_FIELDS)[number];

/** A row of a task's history as words: which sentence, and what fills it. */
export type EventWords =
  | { kind: "created" }
  | { kind: "status"; from: string | null; to: string }
  | { kind: "description" }
  | { kind: "outcome" }
  | { kind: "set"; field: NamedField; from: string | null; to: string }
  | { kind: "cleared"; field: NamedField; from: string }
  | { kind: "people"; field: PeopleField; to: string | null }
  | { kind: "linked"; link: LinkKind; target: string }
  | { kind: "unlinked"; link: LinkKind }
  | { kind: "step"; to: string | null }
  | { kind: "other"; field: string; from: string | null; to: string | null };

function isOne<T extends string>(list: readonly T[], value: string): value is T {
  return (list as readonly string[]).includes(value);
}

export type LinkKind = "srs_item" | "plan_step" | "run" | "pull" | "other";
const LINK_KINDS = ["srs_item", "plan_step", "run", "pull"] as const;

/** A link's row: `srs_item UC ORD-1`, `run 12` when made, `srs_item link 3` when dropped. */
function linkOf(value: string): { link: LinkKind; rest: string } {
  const [kind, ...rest] = value.split(" ");
  return isOne(LINK_KINDS, kind) ? { link: kind, rest: rest.join(" ") } : { link: "other", rest: value };
}

/** How a row of the history reads (evo_agents.hub.server.tasks.writes.record and events). */
export function eventWords(event: Pick<TaskEvent, "field" | "from_value" | "to_value">): EventWords {
  const { field, from_value: from, to_value: to } = event;
  if (field === "created") return { kind: "created" };
  if (field === "description") return { kind: "description" };
  if (field === "outcome") return { kind: "outcome" };
  if (field === "status" && to !== null) return { kind: "status", from, to };
  if (field === "plan_step") return { kind: "step", to };
  if (field === "link") {
    if (to !== null) {
      const made = linkOf(to);
      return { kind: "linked", link: made.link, target: made.link === "run" ? `#${made.rest}` : made.rest };
    }
    if (from !== null) return { kind: "unlinked", link: linkOf(from).link };
  }
  if (isOne(PEOPLE_FIELDS, field)) return { kind: "people", field, to };
  if (isOne(NAMED_FIELDS, field)) {
    if (to !== null) return { kind: "set", field, from, to };
    if (from !== null) return { kind: "cleared", field, from };
  }
  return { kind: "other", field, from, to };
}
