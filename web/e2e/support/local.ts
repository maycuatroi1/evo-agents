import { call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { type Account, bearerClient, machineToken } from "./hub";
import type { Run, WorkerEvent } from "./runs";

/**
 * A member's own Claude Code session on the hub, as its machine reports it through the API (docs/workers.md):
 * registered with its machine, directory and Claude account, its state, the events of its turns, and the question it asks
 * the person. The specs play the machine's hooks. The hub only watches the session and never controls it.
 */
type Options = {
  machine?: string;
  cwd?: string;
  email?: string;
  profile?: string | null;
  title?: string;
};

export type LocalSession = { run: Run; session: string };

/** Register Claude session `session` of `owner`'s machine in `project`; the run of it, new or the owner's own. */
export async function registerLocal(owner: Account, project: string, session: string, options: Options = {}): Promise<LocalSession> {
  const api = bearerClient(await machineToken(owner));
  const body: components["schemas"]["LocalRegister"] = {
    machine: options.machine ?? "laptop-binh",
    cwd: options.cwd ?? "/home/binh/work/api",
    claude_email: options.email ?? `${owner.login}@example.com`,
    claude_profile: options.profile ?? null,
    title: options.title ?? "Fix the queue",
  };
  const run = await call(api.PUT("/v1/projects/{project}/local-runs/{session_id}", { params: { path: { project, session_id: session } }, body }));
  return { run, session };
}

/** The session's state report: running, waiting, idle or ended, and the plan and step it works on. */
export async function localState(
  owner: Account,
  project: string,
  runId: number,
  body: components["schemas"]["LocalState"],
): Promise<void> {
  const api = bearerClient(await machineToken(owner));
  await call(api.POST("/v1/projects/{project}/local-runs/{run_id}/state", { params: { path: { project, run_id: runId } }, body }));
}

/** Events of the session's turns, after those stored. */
export async function localEvents(owner: Account, project: string, runId: number, events: WorkerEvent[], from = 0): Promise<void> {
  const api = bearerClient(await machineToken(owner));
  const batch = events.map((event, index) => ({
    seq: from + index + 1,
    at: event.at ?? new Date().toISOString(),
    kind: event.kind as components["schemas"]["EventIn"]["kind"],
    body: event.body,
  }));
  await call(api.POST("/v1/projects/{project}/local-runs/{run_id}/events", { params: { path: { project, run_id: runId } }, body: { events: batch } }));
}

/** The question the session asks its person; the id of the decision. */
export async function localAsk(owner: Account, project: string, runId: number, question: string): Promise<number> {
  const api = bearerClient(await machineToken(owner));
  const decision = await call(
    api.POST("/v1/projects/{project}/local-runs/{run_id}/decisions", {
      params: { path: { project, run_id: runId } },
      body: {
        category: "question",
        question,
        context: "The queue test fails on main.",
        options: [
          { key: "fix", label: "Fix the test" },
          { key: "skip", label: "Skip it" },
        ],
        recommended: "fix",
      },
    }),
  );
  return decision.id;
}
