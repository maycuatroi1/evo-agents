import { call } from "../../src/lib/api/client";

import type { Account } from "./hub";
import { apiOf } from "./memories";
import { enablePlugin } from "./plugins";
import { enableTasks } from "./srs";
import { createSprintByApi, createTaskByApi } from "./tasks";

/**
 * The core plugin pm through the real API (plan pm-daily-tracking): enabled with the two grants its morning job
 * asks for, over a project with an active sprint, a task in each state the report counts, and one blocked by hand.
 */
export function pmPath(project: string, date?: string): string {
  return `/p/${project}/x/pm/daily${date ? `?date=${date}` : ""}`;
}

/** Enable pm in `project` as `account`, an admin of it, with the grants it asks for. */
export async function enablePm(account: Account, project: string) {
  return enablePlugin(account, project, "pm", ["notifications:send", "chats:write"]);
}

/**
 * tasks and pm enabled in `project`, the active Sprint 7, and four tasks in it: T-1 in progress, T-2 in review and T-3 to
 * do, assigned to `account` (T-2 with `account` as QA), and T-4 in progress and blocked by hand on `waiting` (text).
 * Long titles are given so a narrow window shows how they wrap.
 */
export async function seedPm(account: Account, project: string, titles: { long?: string } = {}) {
  await enableTasks(account, project);
  await enablePm(account, project);
  await createSprintByApi(account, project, "Sprint 7", "active");
  const long = titles.long ?? "Checkout places the order";
  await createTaskByApi(account, project, { title: long, status: "in_progress", sprint: "Sprint 7", assignees: [account.login] });
  await createTaskByApi(account, project, { title: "Totals round the wrong way", status: "in_review", sprint: "Sprint 7", roles: { qa: [account.login] } });
  await createTaskByApi(account, project, { title: "Test cases for the cart", status: "todo", sprint: "Sprint 7", assignees: [account.login] });
  await createTaskByApi(account, project, { title: "Payment retries", status: "in_progress", sprint: "Sprint 7", assignees: [account.login] });
  const api = await apiOf(account);
  await call(
    api.POST("/v1/x/tasks/{project}/tasks/{ref}/blockers", {
      params: { path: { project, ref: "T-4" } },
      body: { reason: "Waiting for the gateway keys", waiting_text: "Acme bank" },
    }),
  );
}
