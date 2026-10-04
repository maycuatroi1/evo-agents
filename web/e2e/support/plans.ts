import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

import type { Page } from "@playwright/test";

import { type ApiClient, call } from "../../src/lib/api/client";

import { type Account, bearerClient, machineToken } from "./hub";

/**
 * Plans for the plan pages to show, pushed through the real API as a writer of the project, the way
 * `evo-agents hub plan import` and `evo harness step` do it. The bodies are the redacted copies of real plans in
 * tests/hub/fixtures/plans (Vietnamese prose, folded text, evidence), read by the package's own YAML loader.
 */
const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const FIXTURES = path.join(REPO_ROOT, "tests", "hub", "fixtures", "plans", "completed");

type Body = { [key: string]: unknown };
type Step = Body & { id: number; status?: string };

export function loadFixture(name: string): Body {
  const python = process.env.PYTHON ?? "python3";
  const script = "import json, sys; from evo_agents.harness import load_yaml; print(json.dumps(load_yaml(sys.argv[1])))";
  const out = execFileSync(python, ["-c", script, path.join(FIXTURES, `${name}.yaml`)], {
    cwd: REPO_ROOT,
    env: { ...process.env, PYTHONPATH: [REPO_ROOT, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter) },
    encoding: "utf8",
  });
  return JSON.parse(out) as Body;
}

/** The completed plan of the fixtures: every one of its 13 steps is done. */
export const COMPLETED_PLAN = "kg-prototype";
export const COMPLETED_STEPS = 13;
/** The active plan: kg-assertion-layer with steps 10 to 20 reopened, then moved on in two revisions. */
export const ACTIVE_PLAN = "kg-assertion-layer";
export const ACTIVE_STEPS = 20;
/** Step 10 is marked done in revision 2 with this evidence (two lines, the second indented). */
export const EVIDENCE_STEP = "10";
export const EVIDENCE = "evo-agents@4f2c1d9: pytest tests/kg 212 passed, ruff sạch.\n  Chạy lại trên meridai-harness: 0 lỗi, 3 cảnh báo về nhãn.";
export const DONE_AT = "2026-10-04T15:20:00+07:00";
/** After revision 3: 10 done, step 11 in progress, step 12 blocked, the other 8 pending. */
export const ACTIVE_DONE = 10;

/** The lines revision 2 changes in the plan's copy, as the diff must show them. */
export const CHANGED_LINES = {
  removed: ["    status: in_progress"],
  added: [
    "    status: done",
    `    done_at: '${DONE_AT}'`,
    "    evidence: |-",
    "      evo-agents@4f2c1d9: pytest tests/kg 212 passed, ruff sạch.",
    "        Chạy lại trên meridai-harness: 0 lỗi, 3 cảnh báo về nhãn.",
  ],
};

function reopened(body: Body): Body {
  const steps = (body.steps as Step[]).map((step) => {
    if (step.id < 10) return step;
    const { done_at: _doneAt, evidence: _evidence, ...rest } = step;
    const status = step.id === 10 ? "in_progress" : step.id === 11 || step.id === 12 ? "blocked" : "pending";
    return { ...rest, status };
  });
  return { ...body, steps };
}

async function put(api: ApiClient, project: string, body: Body, area: "active" | "completed") {
  return call(
    api.PUT("/v1/projects/{project}/plans/{plan_id}", {
      params: { path: { project, plan_id: String(body.id) } },
      body: { body, area },
    }),
  );
}

async function patchStep(api: ApiClient, project: string, plan: string, step: number, updates: Record<string, string>, revision: number) {
  return call(
    api.PATCH("/v1/projects/{project}/plans/{plan_id}", {
      params: { path: { project, plan_id: plan } },
      body: { section: "steps", step, updates, if_revision: revision },
    }),
  );
}

export type Seeded = { project: string; revisions: number };

/** Push the completed plans and the active one (3 revisions) into `project`, as `writer` (a writer there). */
export async function seedPlans(writer: Account, project: string): Promise<Seeded> {
  const api = bearerClient(await machineToken(writer));
  await put(api, project, loadFixture(COMPLETED_PLAN), "completed");
  await put(api, project, loadFixture("evo-lms-migration"), "completed");
  const first = await put(api, project, reopened(loadFixture(ACTIVE_PLAN)), "active");
  const second = await patchStep(
    api,
    project,
    ACTIVE_PLAN,
    10,
    { status: "done", done_at: DONE_AT, evidence: EVIDENCE },
    first.revision,
  );
  const third = await patchStep(api, project, ACTIVE_PLAN, 11, { status: "in_progress" }, second.revision);
  return { project, revisions: third.revision };
}

/**
 * Open `url` and wait until React has put every streamed part of the page in place: until then a streamed part sits
 * in a hidden <div id="S:n"> beside its loading fallback, and a test id can match twice.
 */
export async function open(page: Page, url: string): Promise<void> {
  await page.goto(url);
  await page.waitForFunction(() => document.querySelector('div[hidden][id^="S:"]') === null);
}
