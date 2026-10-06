import { call } from "../../src/lib/api/client";
import fixture from "../../src/test/fixtures/plan-run-waiting.json" with { type: "json" };

import { type Account, bearerClient, machineToken } from "./hub";
import {
  askDecisionWith,
  claimRun,
  dispatchPlan,
  liveWorker,
  type LiveWorker,
  reportState,
  reportStepWith,
  type Run,
  runOf,
  sendEvents,
  type WorkerEvent,
  workerHeartbeat,
} from "./runs";

/**
 * A plan run replayed from what a real one sent: run #8 of the production hub (src/test/fixtures/plan-run-waiting.json),
 * a Claude Code plan run that did steps 1 and 2, asked its owner decision #1 on step 3 and waits. The spec plays its
 * worker through the API in the order the hub got it: the events the worker sent go up as they were, and what the hub
 * wrote itself (moves, step reports, the decision) comes from the same calls the worker made.
 */
export const REAL_PLAN = fixture.plan.plan_id;
export const REAL_REPO = fixture.run.repos[0].repo;
export const REAL_EVENTS = fixture.events.events;
export const REAL_DECISION = fixture.decisions.decisions[0];
export const REAL_STEPS = fixture.plan.body.steps;

type FixtureEvent = (typeof REAL_EVENTS)[number];
type Body = Record<string, unknown>;

/** The plan as the run claimed it: every step pending. */
function planAtClaim() {
  const body = structuredClone(fixture.plan.body) as Body & { steps: Body[] };
  body.steps = body.steps.map((step) => {
    const { evidence: _evidence, done_at: _doneAt, note: _note, ...rest } = step;
    return { ...rest, status: "pending" };
  });
  return body;
}

/** Push the plan into `project` as `owner`, a writer there, as it stood when the run claimed it. */
export async function seedRealPlan(owner: Account, project: string): Promise<void> {
  const api = bearerClient(await machineToken(owner));
  await call(
    api.PUT("/v1/projects/{project}/plans/{plan_id}", {
      params: { path: { project, plan_id: REAL_PLAN } },
      body: { body: planAtClaim(), area: "active" },
    }),
  );
}

function stepReport(body: Body): { key: string; report: Body } {
  const report = body.step_report as { step: string; status: string; repo: string; commit_sha: string | null };
  const step = REAL_STEPS.find((item) => String(item.id) === report.step);
  if (report.status !== "done") return { key: report.step, report: { status: report.status, repo: report.repo } };
  return {
    key: report.step,
    report: {
      status: "done",
      repo: report.repo,
      commit_sha: report.commit_sha,
      evidence: step?.evidence ?? `step ${report.step} verified`,
      verify: [{ command: step?.verify ?? "true", exit_code: 0, duration_ms: 10 }],
    },
  };
}

/**
 * Dispatch the plan run to a fresh worker of `owner` and replay the real run up to the point it waits. Returns the
 * run as the hub holds it then, and the decision it waits on.
 */
export async function replayWaitingRun(owner: Account, project: string, name: string): Promise<{ live: LiveWorker; run: Run; decision: number }> {
  const live = await liveWorker(owner, project, name, 1, { report: { repos: [REAL_REPO] } });
  const queued = await dispatchPlan(owner, project, { plan_id: REAL_PLAN, worker_id: live.worker.id, runtime: "claude-code", timeout_h: 4 });
  const claimed = await claimRun(live);
  if (claimed?.id !== queued.id) throw new Error(`the worker claimed ${claimed?.id ?? "nothing"}, not plan run #${queued.id}`);
  await workerHeartbeat(live, [queued.id], { repos: [REAL_REPO] });

  let pending: WorkerEvent[] = [];
  const flush = async () => {
    if (pending.length) await sendEvents(live, queued.id, pending);
    pending = [];
  };
  let decision = 0;
  for (const event of REAL_EVENTS as FixtureEvent[]) {
    const body = event.body as Body;
    if (event.kind === "state") {
      await flush();
      const to = body.to as string;
      if (to === "running") await reportState(live, queued.id, { state: "running", session_id: fixture.run.session_id });
      else if (to === "waiting") await reportState(live, queued.id, { state: "waiting" });
      // leased: the claim above
    } else if (event.kind === "system" && body.step_report) {
      await flush();
      const { key, report } = stepReport(body);
      await reportStepWith(live, queued.id, key, report);
    } else if (event.kind === "system" && body.decision) {
      await flush();
      decision = await askDecisionWith(live, queued.id, {
        category: REAL_DECISION.category,
        question: REAL_DECISION.question,
        context: REAL_DECISION.context,
        options: REAL_DECISION.options.map(({ key, label, description }) => ({ key, label, description })),
        recommended: REAL_DECISION.recommended,
        step_key: REAL_DECISION.step_key,
      });
    } else {
      pending.push({ kind: event.kind, body, at: event.at });
    }
  }
  await flush();
  return { live, run: await runOf(owner, project, queued.id), decision };
}
