import { queryOptions } from "@tanstack/react-query";

import { call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

export { OPPORTUNITY_REF, OPPORTUNITY_STATUSES, type OpportunityStatus, opportunitiesHref } from "@/lib/opportunity-links";

/**
 * The reads of a project's opportunities (plan opportunity-intake; `evo_agents/hub/server/opportunities.py`): what the
 * project could win, by code O-<number>. Any member with a grant reads them; the writes, which only the Opportunities
 * pages make, are `lib/opportunity-writes.ts`.
 */
type Schemas = components["schemas"];
export type ProjectOpportunity = Schemas["ProjectOpportunity"];
export type ProjectOpportunityShown = Schemas["ProjectOpportunityShown"];
export type OpportunityRevision = Schemas["OpportunityRevision"];
export type OpportunityEvidence = Schemas["OpportunityEvidence"];

export const opportunityKeys = {
  /** Every read of a project's opportunities: invalidated after a write of one. */
  all: (project: string) => ["projects", project, "opportunities"] as const,
  list: (project: string) => ["projects", project, "opportunities", "list"] as const,
  one: (project: string, code: string) => ["projects", project, "opportunities", "one", code] as const,
  revisions: (project: string, code: string) => ["projects", project, "opportunities", "revisions", code] as const,
  evidence: (project: string, code: string) => ["projects", project, "opportunities", "evidence", code] as const,
};

/** The project's opportunities as they are now, of every status. */
export const opportunitiesQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: opportunityKeys.list(project),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/opportunities", { params: { path: { project } }, signal })),
  });

/** One opportunity at its newest revision, with its evidence counted. */
export const opportunityQuery = (api: ApiSource, project: string, code: string) =>
  queryOptions({
    queryKey: opportunityKeys.one(project, code),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/opportunities/{ref}", { params: { path: { project, ref: code } }, signal })),
  });

/** Every revision of an opportunity, newest first, each with the fields it changed. */
export const opportunityRevisionsQuery = (api: ApiSource, project: string, code: string) =>
  queryOptions({
    queryKey: opportunityKeys.revisions(project, code),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/opportunities/{ref}/revisions", { params: { path: { project, ref: code } }, signal })),
  });

/** The lines of evidence of an opportunity, by number. */
export const opportunityEvidenceQuery = (api: ApiSource, project: string, code: string) =>
  queryOptions({
    queryKey: opportunityKeys.evidence(project, code),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/opportunities/{ref}/evidence", { params: { path: { project, ref: code } }, signal })),
  });
