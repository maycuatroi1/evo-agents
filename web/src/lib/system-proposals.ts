import { queryOptions } from "@tanstack/react-query";

import { call } from "@/lib/api/client";
import type { ApiSource } from "@/lib/queries";
import { specOf, type Spec } from "@/lib/systems";
import { type BuilderProposal, waitingKeys } from "@/lib/waiting-proposals";

/**
 * The proposals of @builder of kind `system` (`evo_agents/core_plugins/builder/system_changes.py`): the spec
 * evo-agent-system/1 an agent system would have, with the agent profiles and named graphs it needs made first. They
 * wait for an admin of the project, who accepts or discards one in the designer's Builder panel; a writer's list of
 * them is empty. A project that does not enable @builder answers 404.
 */
export type NewAgent = { handle: string; name: string };
export type NewGraph = { name: string; description: string };
export type SystemProposal = {
  id: number;
  summary: string;
  baseVersion: number;
  spec: Spec;
  agents: NewAgent[];
  graphs: NewGraph[];
};

export const systemProposalKeys = {
  of: (project: string) => [...waitingKeys.all(project), "system"] as const,
};

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown) => (typeof value === "string" ? value : "");

/** What a waiting proposal of kind system sets, read defensively. */
export function systemProposalOf(proposal: BuilderProposal): SystemProposal {
  const sets = isRecord(proposal.change.set) ? proposal.change.set : {};
  const agents = Array.isArray(sets.agents) ? sets.agents.filter(isRecord) : [];
  const graphs = Array.isArray(sets.graphs) ? sets.graphs.filter(isRecord) : [];
  return {
    id: proposal.id,
    summary: proposal.summary,
    baseVersion: proposal.base_version,
    spec: specOf(sets.spec),
    agents: agents.map((agent) => ({ handle: text(agent.handle), name: text(agent.name) })),
    graphs: graphs.map((graph) => ({ name: text(graph.name), description: text(graph.description) })),
  };
}

/** The waiting proposals of kind system the visitor may decide, newest first. */
export const systemProposalsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: systemProposalKeys.of(project),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/x/builder/{project}/proposals/waiting", { params: { path: { project }, query: { kind: "system" } }, signal })),
    select: (proposals) => [...proposals].sort((a, b) => b.id - a.id),
    retry: false,
  });

/** The newest waiting proposal for the system `slug`, or null. */
export function proposalFor(proposals: readonly BuilderProposal[] | undefined, slug: string): SystemProposal | null {
  const found = proposals?.find((proposal) => proposal.target.system === slug);
  return found ? systemProposalOf(found) : null;
}
