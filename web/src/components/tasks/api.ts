import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import { TASKS_PLUGIN } from "@/lib/core-plugin-links";
import type { ApiSource } from "@/lib/queries";

/**
 * The reads and writes of the core plugin tasks (evo_agents.hub.server.tasks.server, under /v1/x/tasks/{project}/),
 * as TanStack Query options the server (prefetch) and the browser share, and the writes the browser makes with the
 * session's X-Evo-CSRF header. Every key sits under the project's plugin keys; the lists of tasks share one prefix, so
 * a write reloads every list at once.
 */
type Schemas = components["schemas"];
export type TasksSettings = Schemas["TasksSettings"];
export type TaskStatus = Schemas["TasksStatus"];
export type TaskGroup = TaskStatus["group"];
export type Task = Schemas["TasksItem"];
export type TaskDetail = Schemas["TasksItemDetail"];
export type TaskLink = Schemas["TasksLink"];
export type TaskEvent = Schemas["TasksEvent"];
export type TaskComment = Schemas["TasksComment"];
export type TaskBlocker = Schemas["TasksBlocker"];
export type TaskBlockerIn = Schemas["TasksBlockerIn"];
export type TaskBlockerEdit = Schemas["TasksBlockerEdit"];
export type TaskPerson = Schemas["TasksPerson"];
export type Sprint = Schemas["TasksSprint"];
export type SprintState = Sprint["state"];
export type Priority = Task["priority"];
export type TaskIn = Schemas["TasksItemIn"];
export type TaskEdit = Schemas["TasksItemEdit"];
export type SprintIn = Schemas["TasksSprintIn"];
export type SprintEdit = Schemas["TasksSprintEdit"];
export type TasksImport = Schemas["TasksImport"];
export type TasksImportResult = Schemas["TasksImportResult"];
export type Role = keyof Task["roles"];

export const PLUGIN = TASKS_PLUGIN;
export const PRIORITIES: readonly Priority[] = ["urgent", "high", "medium", "low"];
export const ROLES: readonly Role[] = ["ba", "qa", "dev"];
/** The most tasks one list asks for (evo_agents.hub.server.tasks.store.MAX_LISTED). */
export const MAX_LISTED = 2_000;

/** Which tasks a list holds: every task, a sprint's, those of no sprint, or the caller's own. */
export type TaskList = { kind: "all" } | { kind: "sprint"; name: string } | { kind: "backlog" } | { kind: "mine" };

export const tasksKeys = {
  all: (project: string) => ["projects", project, "plugins", "x", PLUGIN] as const,
  settings: (project: string) => [...tasksKeys.all(project), "settings"] as const,
  people: (project: string) => [...tasksKeys.all(project), "people"] as const,
  sprints: (project: string) => [...tasksKeys.all(project), "sprints"] as const,
  lists: (project: string) => [...tasksKeys.all(project), "list"] as const,
  list: (project: string, list: TaskList) => [...tasksKeys.lists(project), list] as const,
  task: (project: string, ref: string) => [...tasksKeys.all(project), "task", ref] as const,
};

export const tasksSettingsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: tasksKeys.settings(project),
    queryFn: ({ signal }) => call(api().GET("/v1/x/tasks/{project}/settings", { params: { path: { project } }, signal })),
  });

export const tasksPeopleQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: tasksKeys.people(project),
    queryFn: ({ signal }) => call(api().GET("/v1/x/tasks/{project}/people", { params: { path: { project } }, signal })),
  });

export const sprintsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: tasksKeys.sprints(project),
    queryFn: ({ signal }) => call(api().GET("/v1/x/tasks/{project}/sprints", { params: { path: { project } }, signal })),
  });

function listQuery(list: TaskList) {
  if (list.kind === "sprint") return { sprint: list.name, limit: MAX_LISTED };
  if (list.kind === "backlog") return { backlog: true, limit: MAX_LISTED };
  if (list.kind === "mine") return { mine: true, limit: MAX_LISTED };
  return { limit: MAX_LISTED };
}

export const tasksQuery = (api: ApiSource, project: string, list: TaskList) =>
  queryOptions({
    queryKey: tasksKeys.list(project, list),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/x/tasks/{project}/tasks", { params: { path: { project }, query: listQuery(list) }, signal })),
  });

export const taskQuery = (api: ApiSource, project: string, ref: string) =>
  queryOptions({
    queryKey: tasksKeys.task(project, ref),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/x/tasks/{project}/tasks/{ref}", { params: { path: { project, ref } }, signal })),
  });

// Writes, each with the session's CSRF header

export async function createTask(api: ApiClient, project: string, body: TaskIn) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/x/tasks/{project}/tasks", { params: { path: { project } }, body, headers }));
}

export async function editTask(api: ApiClient, project: string, ref: string, body: TaskEdit) {
  const headers = await csrfHeaders(api);
  return call(api.PATCH("/v1/x/tasks/{project}/tasks/{ref}", { params: { path: { project, ref } }, body, headers }));
}

export async function commentOn(api: ApiClient, project: string, ref: string, body: string) {
  const headers = await csrfHeaders(api);
  const params = { path: { project, ref } };
  return call(api.POST("/v1/x/tasks/{project}/tasks/{ref}/comments", { params, body: { body }, headers }));
}

export async function addBlocker(api: ApiClient, project: string, ref: string, body: TaskBlockerIn) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/x/tasks/{project}/tasks/{ref}/blockers", { params: { path: { project, ref } }, body, headers }));
}

export async function editBlocker(api: ApiClient, project: string, ref: string, id: number, body: TaskBlockerEdit) {
  const headers = await csrfHeaders(api);
  const params = { path: { project, ref, blocker_id: id } };
  return call(api.PATCH("/v1/x/tasks/{project}/tasks/{ref}/blockers/{blocker_id}", { params, body, headers }));
}

export async function createSprint(api: ApiClient, project: string, body: SprintIn) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/x/tasks/{project}/sprints", { params: { path: { project } }, body, headers }));
}

export async function editSprint(api: ApiClient, project: string, name: string, body: SprintEdit) {
  const headers = await csrfHeaders(api);
  return call(api.PATCH("/v1/x/tasks/{project}/sprints/{name}", { params: { path: { project, name } }, body, headers }));
}

export async function importTasks(api: ApiClient, project: string, body: TasksImport) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/x/tasks/{project}/import", { params: { path: { project } }, body, headers }));
}
