import { readFileSync } from "node:fs";

import type { Locator, Page } from "@playwright/test";

import { call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { type Account } from "./hub";
import { apiOf } from "./memories";

/**
 * The core plugin tasks through the real API, as `evo-agents hub task create|edit|import` does, with a member's machine
 * token, which needs no CSRF header; and where its pages are. The sample is the Python tests' evo-tasks/1 file
 * (tests/hub/fixtures/tasks/demo-shop-tasks.json, no client's data).
 */
export type TaskIn = components["schemas"]["TasksItemIn"];
export type TaskEdit = components["schemas"]["TasksItemEdit"];
export type TaskDetail = components["schemas"]["TasksItemDetail"];

export const SAMPLE_TASKS = readFileSync(new URL("../../../tests/hub/fixtures/tasks/demo-shop-tasks.json", import.meta.url), "utf8");

export function tasksPath(project: string, page = "board"): string {
  return `/p/${project}/x/tasks/${page}`;
}

export function taskPath(project: string, ref: string): string {
  return tasksPath(project, `board/${encodeURIComponent(ref)}`);
}

/** A task of `project`, created as `account`, a writer; no description and medium priority unless given. */
export async function createTaskByApi(account: Account, project: string, body: Partial<TaskIn> & { title: string }): Promise<TaskDetail> {
  const api = await apiOf(account);
  return call(
    api.POST("/v1/x/tasks/{project}/tasks", { params: { path: { project } }, body: { description_md: "", outcome_md: "", priority: "medium", ...body } }),
  );
}

export async function editTaskByApi(account: Account, project: string, ref: string, body: TaskEdit): Promise<TaskDetail> {
  const api = await apiOf(account);
  return call(api.PATCH("/v1/x/tasks/{project}/tasks/{ref}", { params: { path: { project, ref } }, body }));
}

export async function taskOf(account: Account, project: string, ref: string): Promise<TaskDetail> {
  const api = await apiOf(account);
  return call(api.GET("/v1/x/tasks/{project}/tasks/{ref}", { params: { path: { project, ref } } }));
}

/** A sprint of `project`, created as `account`, a writer; active when asked. */
export async function createSprintByApi(account: Account, project: string, name: string, state: "planned" | "active" = "planned") {
  const api = await apiOf(account);
  return call(
    api.POST("/v1/x/tasks/{project}/sprints", {
      params: { path: { project } },
      body: { name, goal: "Cancel before packing", starts_on: "2026-10-13", ends_on: "2026-10-26", state },
    }),
  );
}

/** The board's column of status `status`. */
export function boardColumn(page: Page, status: string): Locator {
  return page.locator("#main").locator(`[data-testid="board-column"][data-status="${status}"]`);
}

/** The card of task `ref`, wherever it is on the page. */
export function taskCard(page: Page, ref: string): Locator {
  return page.locator("#main").locator(`[data-testid="task-card"][data-task="${ref}"]`);
}
