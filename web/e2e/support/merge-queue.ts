import { call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { STACK_URL } from "./env";
import { type Account, bearerClient, type HubAdmin, machineToken } from "./hub";

/**
 * The merge queue on GitLab and the autonomy policy of a project (plan gitlab-merge-queue) for the specs: the
 * project's repos on a GitLab of the fictional northwind group, its policy written through the real API as
 * `evo-agents hub policy set` does, and its queue as the hub's job would leave it (hub_stack POST /merge-queue/seed,
 * `e2e/merge_seed.py`), since no GitLab answers the stack.
 */
export type PolicyWritten = components["schemas"]["PolicyWritten"];
export type MergeSeed = { requests: Record<string, number>; sample: number; answered: number };

export const REPO = "shop";
export const OTHER = "billing";
export const GITHUB_REPO = "api";

/** List `shop` and `billing` on GitLab among the project's repos, beside its `api` on github.com. */
export async function gitlabRepos(admin: HubAdmin, project: string): Promise<void> {
  for (const name of [REPO, OTHER]) {
    await admin.addRepo(project, { name, origin: `https://gitlab.example.org/northwind/${name}`, default_branch: "main", path: name });
  }
}

/** The next revision of the project's policy, written by `account` (an admin of the project) with its machine token. */
export async function putPolicy(account: Account, project: string, policy: Record<string, unknown>, note: string | null = null): Promise<PolicyWritten> {
  const api = bearerClient(await machineToken(account));
  return call(api.PUT("/v1/projects/{project}/policy", { params: { path: { project } }, body: { policy, note } }));
}

/** Two revisions: shop in auto first, then its PROD release by hand and a UAT check job. */
export async function seedPolicy(account: Account, project: string): Promise<void> {
  await putPolicy(account, project, { repos: { [REPO]: { mode: "auto" } } }, "shop joins the queue");
  await putPolicy(
    account,
    project,
    { repos: { [REPO]: { mode: "auto", prod_release: "manual", jobs: { uat_check: "uat-check" } } }, breaker: { reverts_in_a_row: 3 } },
    "release shop by hand",
  );
}

/** The project's queue as the hub's job would leave it; `notify` gets the notices the hub sends the admins. */
export async function seedMergeQueue(project: string, { admin, notify }: { admin?: string; notify?: string } = {}): Promise<MergeSeed> {
  const response = await fetch(`${STACK_URL}/merge-queue/seed`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ project, repo: REPO, other: OTHER, admin, notify }),
  });
  if (!response.ok) throw new Error(`hub_stack /merge-queue/seed: ${response.status} ${await response.text()}`);
  return (await response.json()) as MergeSeed;
}

export function mergeQueuePath(project: string, rest = ""): string {
  return `/p/${project}/merge-queue${rest}`;
}
