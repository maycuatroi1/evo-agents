import { keepPreviousData, queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { projectHref } from "@/components/shell/nav";
import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import { isApiError } from "@/lib/api/errors";
import type { components, operations } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * What the Curator pages read and write (docs/curator.md, docs/hub.md): a project's night shift (its charter, its
 * schedules, the night now and the nights before), the proposals and findings its review runs wrote, and the session
 * digests their evidence cites. Any member with a grant reads them; only an admin of the project writes the charter
 * and answers a proposal, and an admin or the owner of one of its schedules pauses and resumes the night shift. The
 * API decides again on every write.
 */
type Schemas = components["schemas"];
export type CuratorStatus = Schemas["CuratorStatus"];
export type Charter = Schemas["Charter"];
export type CharterWrite = Schemas["CharterWrite"];
export type CharterRevision = Schemas["CharterRevision"];
export type NightList = Schemas["NightList"];
export type NightSummary = Schemas["NightSummary"];
export type Proposal = Schemas["Proposal"];
export type ProposalSummary = Schemas["ProposalSummary"];
export type ProposalList = Schemas["ProposalList"];
export type ProposalState = ProposalSummary["state"];
export type ProposalAnswer = Schemas["ProposalAnswer"];
export type ProposalAction = ProposalAnswer["action"];
export type Finding = Schemas["Finding"];
export type Ledger = Schemas["Ledger"];
export type LedgerLine = Schemas["LedgerLine"];
export type DigestRecord = Schemas["DigestRecord"];
export type CuratorState = NonNullable<CuratorStatus["state"]>;
export type Runtime = Schemas["Role"]["runtime"];
/** The lenses the API names: `review.LENSES`, in the order the nights take them. */
export type Lens = NonNullable<
  NonNullable<operations["list_proposals_v1_projects__project__curator_proposals_get"]["parameters"]["query"]>["lens"]
>;

/** The page reads the night shift again this often while a run of it is in flight, and this often otherwise. */
export const CURATOR_LIVE_MS = 10_000;
export const CURATOR_IDLE_MS = 60_000;
/** Proposals on one page of the list; the API allows up to 200. */
export const PROPOSALS_PAGE = 50;
/** The nights the Curator's page lists. */
export const NIGHTS_SHOWN = 30;

export const curatorKeys = {
  /** Everything the Curator pages read of a project: invalidated after a pause, a resume or a charter write. */
  all: (project: string) => ["projects", project, "curator"] as const,
  status: (project: string) => ["projects", project, "curator", "status"] as const,
  charter: (project: string, revision: number | null) => ["projects", project, "curator", "charter", revision] as const,
  revisions: (project: string) => ["projects", project, "curator", "revisions"] as const,
  nights: (project: string, limit: number) => ["projects", project, "curator", "nights", limit] as const,
  /** Every read of the project's proposals: invalidated after an answer. */
  proposals: (project: string) => ["projects", project, "curator", "proposals"] as const,
  proposalList: (project: string, query: ProposalQuery) => ["projects", project, "curator", "proposals", "list", query] as const,
  proposal: (project: string, id: number) => ["projects", project, "curator", "proposals", "one", id] as const,
  /** A proposal's ledger, below its proposals: an answer adds a line to it. */
  ledger: (project: string, id: number) => ["projects", project, "curator", "proposals", "ledger", id] as const,
  finding: (project: string, id: number) => ["projects", project, "curator", "findings", id] as const,
  digest: (project: string, session: string) => ["projects", project, "digests", session] as const,
};

export const CURATOR_SEGMENT = "curator";

export function curatorHref(project: string, page: "" | "proposals" | "charter" = ""): Route {
  return projectHref(project, page ? `${CURATOR_SEGMENT}/${page}` : CURATOR_SEGMENT);
}

/** The project's proposals that wait for an answer, as Home and the Curator's figures link to them. */
export function proposalsWaitingHref(project: string): Route {
  return `${curatorHref(project, "proposals")}?state=open` as Route;
}

export function proposalHref(project: string, id: number): Route {
  return projectHref(project, `${CURATOR_SEGMENT}/proposals/${id}`);
}

/** The night shift: read every 10 seconds while a run of it is in flight, every minute otherwise. */
export const curatorStatusQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: curatorKeys.status(project),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/curator", { params: { path: { project } }, signal })),
    refetchInterval: (query) => (query.state.data?.night?.active_run_id ? CURATOR_LIVE_MS : CURATOR_IDLE_MS),
  });

/**
 * The charter at its newest revision, or at `revision`. A project without one answers 404, which is data here (no
 * charter yet), not a failure: the query returns null for it.
 */
export const charterQuery = (api: ApiSource, project: string, revision: number | null = null) =>
  queryOptions({
    queryKey: curatorKeys.charter(project, revision),
    queryFn: async ({ signal }) => {
      try {
        return await call(
          api().GET("/v1/projects/{project}/curator/charter", {
            params: { path: { project }, query: revision === null ? {} : { revision } },
            signal,
          }),
        );
      } catch (error) {
        if (revision === null && isApiError(error) && error.status === 404 && /has no charter/.test(error.info.message)) return null;
        throw error;
      }
    },
  });

export const charterRevisionsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: curatorKeys.revisions(project),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/curator/charter/revisions", { params: { path: { project } }, signal })),
  });

export const nightsQuery = (api: ApiSource, project: string, limit = NIGHTS_SHOWN) =>
  queryOptions({
    queryKey: curatorKeys.nights(project, limit),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/curator/nights", { params: { path: { project }, query: { limit } }, signal })),
    refetchInterval: CURATOR_IDLE_MS,
  });

/** The filters of GET .../curator/proposals that the list uses. */
export type ProposalQuery = {
  state: ProposalState | null;
  tier: number | null;
  lens: Lens | null;
  run: number | null;
  limit: number;
  offset: number;
};

export const proposalsQuery = (api: ApiSource, project: string, query: ProposalQuery) =>
  queryOptions({
    queryKey: curatorKeys.proposalList(project, query),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/curator/proposals", {
          params: {
            path: { project },
            query: {
              ...(query.state ? { state: [query.state] } : {}),
              ...(query.tier !== null ? { tier: [query.tier] } : {}),
              ...(query.lens ? { lens: query.lens } : {}),
              ...(query.run !== null ? { run_id: query.run } : {}),
              limit: query.limit,
              offset: query.offset,
            },
          },
          signal,
        }),
      ),
    placeholderData: keepPreviousData, // the last page stays on screen while the next filter or page loads
    refetchInterval: CURATOR_IDLE_MS,
  });

export const proposalQuery = (api: ApiSource, project: string, id: number) =>
  queryOptions({
    queryKey: curatorKeys.proposal(project, id),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/curator/proposals/{proposal_id}", {
          params: { path: { project, proposal_id: id } },
          signal,
        }),
      ),
  });

/** What happened to a proposal, line by line, oldest first; the hub only ever adds lines. */
export const ledgerQuery = (api: ApiSource, project: string, id: number) =>
  queryOptions({
    queryKey: curatorKeys.ledger(project, id),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/curator/proposals/{proposal_id}/ledger", {
          params: { path: { project, proposal_id: id } },
          signal,
        }),
      ),
  });

export const findingQuery = (api: ApiSource, project: string, id: number) =>
  queryOptions({
    queryKey: curatorKeys.finding(project, id),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/curator/findings/{finding_id}", {
          params: { path: { project, finding_id: id } },
          signal,
        }),
      ),
    staleTime: Infinity, // a finding never changes once recorded
  });

export const digestQuery = (api: ApiSource, project: string, session: string) =>
  queryOptions({
    queryKey: curatorKeys.digest(project, session),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/digests/{session_id}", {
          params: { path: { project, session_id: session } },
          signal,
        }),
      ),
  });

// Writes, each with the session's X-Evo-CSRF header.

export async function pauseCurator(api: ApiClient, project: string): Promise<CuratorStatus> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/curator/pause", { params: { path: { project } }, headers }));
}

export async function resumeCurator(api: ApiClient, project: string): Promise<CuratorStatus> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/curator/resume", { params: { path: { project } }, headers }));
}

export async function writeCharter(api: ApiClient, project: string, body: CharterWrite): Promise<Charter> {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/projects/{project}/curator/charter", { params: { path: { project } }, body, headers }));
}

export async function answerProposal(api: ApiClient, project: string, id: number, body: ProposalAnswer): Promise<Proposal> {
  const headers = await csrfHeaders(api);
  return call(
    api.POST("/v1/projects/{project}/curator/proposals/{proposal_id}/answer", {
      params: { path: { project, proposal_id: id } },
      body,
      headers,
    }),
  );
}
