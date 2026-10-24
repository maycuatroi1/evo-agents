import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { ApiSource } from "@/lib/queries";

import { kgKeys } from "./queries";
import type { DecisionKind, KgDecision, KgNode, NewKgDecision } from "./types";

/** What a writer takes on a node, in the order the forms show them (`evo_agents.hub.server.kg_decisions`). */
export const DECISION_KINDS: readonly DecisionKind[] = ["deprecate", "merge", "link"];
/** The longest reason the hub keeps (`MAX_REASON_CHARS` of `evo_agents.hub.kg_decisions`). */
export const MAX_REASON = 1000;

export type DecisionStep = "accept" | "reject" | "undo";

/** The decisions that name one of `ids`, as their subject or as the other node, in the hub's order (newest first). */
export function decisionsAbout(decisions: readonly KgDecision[], ids: readonly string[]): KgDecision[] {
  const wanted = new Set(ids);
  return decisions.filter((d) => wanted.has(d.src) || (d.dst !== null && wanted.has(d.dst)));
}

/** A decision in one line: `a = b` for a merge, `a -rel-> b` for a link or an edge, the node's id otherwise. */
export function decisionWhat(decision: Pick<KgDecision, "kind" | "src" | "rel" | "dst">): string {
  if (decision.kind === "merge") return `${decision.src} = ${decision.dst ?? ""}`;
  if (decision.rel !== null) return `${decision.src} -${decision.rel}-> ${decision.dst ?? ""}`;
  return decision.src;
}

/** The steps a writer may take on a decision: accept or reject a proposal, undo what is in force or orphaned. */
export function decisionSteps(decision: Pick<KgDecision, "state">): DecisionStep[] {
  if (decision.state === "proposed") return ["accept", "reject"];
  if (decision.state === "active" || decision.state === "orphan") return ["undo"];
  return [];
}

/** The review page's share: the proposals to accept or reject, and the decisions whose subject left the graph. */
export function decisionsToReview(decisions: readonly KgDecision[]): { proposed: KgDecision[]; orphans: KgDecision[] } {
  return {
    proposed: decisions.filter((d) => d.state === "proposed"),
    orphans: decisions.filter((d) => d.state === "orphan"),
  };
}

/** The deprecation the last build marked on a node (`props.deprecated`), or null. */
export function nodeDeprecation(node: Pick<KgNode, "props">): { reason: string; decision: number } | null {
  const found = node.props.deprecated;
  if (!found || typeof found !== "object") return null;
  const { reason, decision } = found as { reason?: unknown; decision?: unknown };
  return typeof reason === "string" && typeof decision === "number" ? { reason, decision } : null;
}

/** The decisions on the graph the reader can see, the revision to write against, and whether they may decide. */
export const kgDecisionsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: kgKeys.decisions(project),
    queryFn: ({ signal }) => call(api().GET("/v1/kg/{project}/decisions", { params: { path: { project } }, signal })),
  });

/** A writer deprecates, merges or links; the next build applies it. A stale `base_rev` is refused with 409. */
export async function takeDecision(api: ApiClient, project: string, body: NewKgDecision) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/kg/{project}/decisions", { params: { path: { project } }, body, headers }));
}

/** A writer accepts or rejects a proposal, or undoes a decision, with a reason and the revision they read. */
export async function stepDecision(
  api: ApiClient,
  project: string,
  number: number,
  step: DecisionStep,
  body: { reason: string; base_rev: number },
) {
  const headers = await csrfHeaders(api);
  return call(
    api.POST("/v1/kg/{project}/decisions/{number}/{step}", {
      params: { path: { project, number, step } },
      body,
      headers,
    }),
  );
}
