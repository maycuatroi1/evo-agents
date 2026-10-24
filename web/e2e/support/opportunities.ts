import { call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { type Account, bearerClient, machineToken } from "./hub";
import { GOAL, putGoal, seedLoopProject } from "./loop";
import { PROBLEM, putProblemByApi } from "./problems";

/**
 * The opportunities of a project (plan opportunity-intake) through the real API, as `evo-agents hub opportunity set`
 * and `evidence add` write them with a writer's machine token, for the specs of the Opportunities pages. Names are
 * fictional.
 */
export type OpportunityWrite = components["schemas"]["ProjectOpportunityWrite"];
export type EvidenceWrite = components["schemas"]["OpportunityEvidenceWrite"];

/** The day `days` from today as YYYY-MM-DD, in UTC as the page counts it. */
export function dayFromToday(days: number): string {
  return new Date(Date.now() + days * 24 * 3600 * 1000).toISOString().slice(0, 10);
}

/** O-1: a pilot with a regional chain, whose window closes in 20 days and which answers problem P-1. */
export const OPPORTUNITY = {
  statement: "A pilot with the regional chain of grocers",
  beneficiary: "Owners of small shops on lumen-shops",
  leverage: "An introduction from the October trade fair",
  window_note: "the chain picks its pilot partners at the end of the month",
  answers: ["P-1"],
  status: "open",
} satisfies OpportunityWrite;

export function opportunitiesPath(project: string, code = ""): string {
  return `/p/${project}/opportunities${code ? `/${code}` : ""}`;
}

/** Write opportunity `code` as `account`, a writer of the project; a new one with `if_revision` 0. */
export async function putOpportunityByApi(account: Account, project: string, code: string, body: OpportunityWrite) {
  const api = bearerClient(await machineToken(account));
  return call(api.PUT("/v1/projects/{project}/opportunities/{ref}", { params: { path: { project, ref: code } }, body: { if_revision: 0, ...body } }));
}

/** Add a line of evidence to opportunity `code` as `account`. */
export async function addEvidenceByApi(account: Account, project: string, code: string, body: EvidenceWrite) {
  const api = bearerClient(await machineToken(account));
  return call(api.POST("/v1/projects/{project}/opportunities/{ref}/evidence", { params: { path: { project, ref: code } }, body }));
}

/**
 * `project` as `owner`, an admin of it, with the Loop's project of `seedLoopProject`, problem P-1, and three
 * opportunities: O-1 (window in 20 days, answers P-1, two lines of evidence of two signals), O-2 (no window) and O-3
 * (window in 5 days). Goal G-1 answers O-1.
 */
export async function seedOpportunities(owner: Account, project: string): Promise<void> {
  await seedLoopProject(owner, project);
  await putProblemByApi(owner, project, "P-1", { ...PROBLEM, metric: null });
  await putOpportunityByApi(owner, project, "O-1", { ...OPPORTUNITY, window_until: dayFromToday(20) });
  await putOpportunityByApi(owner, project, "O-2", {
    statement: "Resell the checkout widget to the shop-fitting suppliers",
    beneficiary: "The suppliers' customers",
    leverage: "The widget already runs in three shops",
    status: "open",
  });
  await putOpportunityByApi(owner, project, "O-3", {
    statement: "Join the city's market-day programme",
    beneficiary: "Market-day stallholders",
    leverage: "The programme needs a payment partner this month",
    window_until: dayFromToday(5),
    status: "open",
  });
  await addEvidenceByApi(owner, project, "O-1", {
    source_kind: "messenger",
    source_uri: "messenger://shops/1",
    speaker: "Lan",
    excerpt: "We wish we could sell to the chain",
    signal: "wish",
    label: "internal",
  });
  await addEvidenceByApi(owner, project, "O-1", {
    source_kind: "messenger",
    source_uri: "messenger://shops/2",
    speaker: "Minh",
    excerpt: "The chain asks for a pilot partner before the fair ends",
    signal: "external_demand",
    label: "internal",
  });
  await putGoal(owner, project, "G-1", { ...GOAL, opportunity: "O-1" });
}
