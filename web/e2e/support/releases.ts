import { call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { STACK_URL } from "./env";
import type { Account } from "./hub";
import { apiOf } from "./memories";
import { registerWorker } from "./workers";

/**
 * A project's daily release for the Releases specs: an admin sets the schedule through the real API, as `evo-agents
 * hub release set` does (the project's only repo on GitHub, the defaults otherwise), and the stack plays the release
 * job that the hub, which has no GitHub App here, cannot (hub_stack POST /releases/seed, e2e/release_seed.py): two
 * cycles that ended and one open now, whose tag v0.25.0 is red on one test and whose fix run is queued, or stopped on
 * that test with a decision for its owner.
 */
export type ReleaseStatus = components["schemas"]["ReleaseStatus"];

/** The test the red tag failed, as the stack's seed records it. */
export const RED_TEST = "tests/hub/test_paths.py::test_a_written_file_ends_lines_in_lf[windows]";
/** The workflow run of the tags, as the stack's seed records it. */
export const WORKFLOW_URL = "https://github.com/example-org/api/actions/runs/7001";

export type SeededRelease = { published: number; closed: number; cycle: number; cut: number; fix: number | null };

async function stack<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${STACK_URL}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`hub_stack ${path}: ${response.status} ${await response.text()}`);
  return (await response.json()) as T;
}

/** `owner`, an admin of `project`, sets its release schedule with the defaults, and becomes its owner. */
export async function setSchedule(owner: Account, project: string): Promise<ReleaseStatus> {
  const api = await apiOf(owner);
  return call(api.PUT("/v1/projects/{project}/releases/schedule", { params: { path: { project } }, body: {} }));
}

/** The schedule set by `owner`, then the cycles the stack seeds for it, their runs ended on a worker of `owner`. */
export async function seedRelease(owner: Account, project: string, { stopped = false }: { stopped?: boolean } = {}): Promise<SeededRelease> {
  await setSchedule(owner, project);
  const worker = await registerWorker(owner, { name: `${project}-release`, projects: [project] });
  return stack<SeededRelease>("/releases/seed", { project, worker_id: worker.id, stopped });
}
