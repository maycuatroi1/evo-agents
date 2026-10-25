import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * The proposals of @builder that wait for a writer's answer (`evo_agents/core_plugins/builder/server.py`, GET
 * /v1/x/builder/{project}/proposals/waiting): a new problem or opportunity, or lines of evidence of one. Those an
 * intake made have no thread to answer in, so the Problems and Opportunities pages list them for a writer, who approves
 * or rejects each there. The routes are the core plugin builder's: a project that does not enable it answers 404.
 */
type Schemas = components["schemas"];
export type BuilderProposal = Schemas["BuilderProposal"];
export type ProposalAnswer = Schemas["BuilderProposalAnswer"]["answer"];
/** The kinds a writer decides, each listed on its own page. */
export type WaitingKind = "problem" | "opportunity";

/** A line of evidence a proposal carries, as `problem_evidence.ProblemEvidenceWrite` dumps it. */
export type ProposedLine = {
  source_kind: string;
  source_uri: string;
  excerpt: string;
  signal: string;
  speaker?: string | null;
  locator?: string | null;
};

export const waitingKeys = {
  /** Every list of waiting proposals of a project: invalidated after an answer. */
  all: (project: string) => ["projects", project, "builder", "waiting"] as const,
  of: (project: string, kind: WaitingKind) => ["projects", project, "builder", "waiting", kind] as const,
};

/** The pending proposals of `kind` that the visitor may decide, newest first; none for a reader. */
export const waitingProposalsQuery = (api: ApiSource, project: string, kind: WaitingKind) =>
  queryOptions({
    queryKey: waitingKeys.of(project, kind),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/x/builder/{project}/proposals/waiting", { params: { path: { project }, query: { kind } }, signal })),
  });

/**
 * Approve or reject pending proposal `id`. Approved, the hub makes what it proposes, or answers it `failed` with why it
 * could not; 409 when another writer answered it first.
 */
export async function answerProposal(api: ApiClient, project: string, id: number, answer: ProposalAnswer) {
  const headers = await csrfHeaders(api);
  return call(
    api.POST("/v1/x/builder/{project}/proposals/{proposal_id}/answer", {
      params: { path: { project, proposal_id: id } },
      body: { answer },
      headers,
    }),
  );
}

/** What a proposal makes: a new problem or opportunity, lines of evidence of one, or a change of one. */
export type ProposalMakes = {
  what: "new" | "evidence" | "change";
  code: string | null;
  /** The statement it sets, for a new one or a change of it. */
  statement: string | null;
  /** The fields it sets, as the problem's or opportunity's route names them. */
  sets: Record<string, unknown>;
  lines: ProposedLine[];
};

export function proposalMakes(proposal: BuilderProposal, kind: WaitingKind): ProposalMakes {
  const change = proposal.change as { set?: Record<string, unknown>; unset?: string[]; evidence?: ProposedLine[] };
  const sets = change.set ?? {};
  const lines = Array.isArray(change.evidence) ? change.evidence : [];
  const statement = typeof sets.statement === "string" ? sets.statement : null;
  const base = { code: proposal.target[kind] ?? null, statement, sets, lines };
  if (proposal.base_version === 0) return { what: "new", ...base };
  if (lines.length > 0 && Object.keys(sets).length === 0 && (change.unset ?? []).length === 0) return { what: "evidence", ...base };
  return { what: "change", ...base };
}
