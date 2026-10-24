import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { OpportunityStatus } from "@/lib/opportunity-links";
import type { ProjectOpportunity } from "@/lib/opportunity-queries";

/**
 * The writes of a project's opportunities (plan opportunity-intake): a writer of the project adds one, writes one whole
 * as its next revision, closes one as captured, missed or dropped and reopens one; the agent of a run never writes
 * one. With the hub's limits, so a form refuses what the hub would. Only the Opportunities pages write.
 */
type Schemas = components["schemas"];
export type OpportunityWrite = Schemas["ProjectOpportunityWrite"];
export type OpportunityClose = Schemas["ProjectOpportunityClose"];

export const CLOSED_STATUSES = ["captured", "missed", "dropped"] as const satisfies readonly OpportunityStatus[];

/** The limits of `evo_agents.hub.opportunities`, so a form refuses what the hub would. */
export const OPPORTUNITY_LIMITS = { statement: 2000, text: 2000, reason: 1000, note: 1000, rank: 10_000, answers: 20 } as const;
/** `admin.LOGIN_NAME`: an owner's login. */
export const LOGIN_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/;
/** `opportunities.ANSWER`: P-3, or <project>/P-3 for another project's problem. */
export const ANSWER_REF = /^(?:[a-z0-9][a-z0-9-]{0,99}\/)?P-[1-9][0-9]{0,8}$/;

/**
 * The code the project's next opportunity takes: one after the highest it ever gave, since a code is never given again
 * and an opportunity is never deleted. The hub checks it again (422 for another code).
 */
export function nextOpportunityCode(opportunities: readonly Pick<ProjectOpportunity, "number">[]): string {
  return `O-${opportunities.reduce((highest, one) => Math.max(highest, one.number), 0) + 1}`;
}

/** Write opportunity `code` whole as its next revision: a new one with `if_revision` 0, an edit at the revision the page read. */
export async function writeOpportunity(api: ApiClient, project: string, code: string, body: OpportunityWrite) {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/projects/{project}/opportunities/{ref}", { params: { path: { project, ref: code } }, body, headers }));
}

/** Close an open opportunity as captured, missed or dropped, for a reason, as its next revision. */
export async function closeOpportunity(api: ApiClient, project: string, code: string, body: OpportunityClose) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/opportunities/{ref}/close", { params: { path: { project, ref: code } }, body, headers }));
}

/** The write that reopens a closed opportunity: its fields as they are, open again, without the reason it was closed. */
export function reopening(held: ProjectOpportunity, note: string): OpportunityWrite {
  return {
    statement: held.statement,
    beneficiary: held.beneficiary,
    leverage: held.leverage,
    window_until: held.window_until,
    window_note: held.window_note,
    answers: held.answers,
    metric: held.metric,
    rank: held.rank,
    owner: held.owner,
    status: "open",
    close_reason: null,
    note: note.trim() || null,
    if_revision: held.revision,
  };
}
