import { keepPreviousData, queryOptions } from "@tanstack/react-query";

import { call } from "@/lib/api/client";
import { isApiError } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

import { CURATOR_IDLE_MS, curatorKeys } from "./queries";

/** A night's figures, which the Curator's tabs read too, and so live with the area's other queries. */
export { figuresQuery } from "./queries";

/**
 * What the Curator's History, Graph health and Runtime pages read, and the replays a change's card shows
 * (docs/curator.md, docs/hub.md): the history run and the transcripts the hub holds, the search over every transcript,
 * run log and group chat, the clusters of the sessions and the sessions themselves, the lines of a transcript, the
 * knowledge graph's health over time, a night's figures, and a change's replays. Any member with a grant reads them;
 * the figures, the transcripts' usage and the graph's health also need the project's default label to be visible
 * through its hub sink, and answer 404 otherwise, which the queries that allow it read as "nothing to show".
 */
type Schemas = components["schemas"];
export type HistoryStatus = Schemas["HistoryStatus"];
export type HistoryRunView = Schemas["HistoryRunView"];
export type TranscriptUsage = Schemas["TranscriptUsage"];
export type FacetClusterList = Schemas["FacetClusterList"];
export type FacetCluster = Schemas["FacetCluster"];
export type HistorySessions = Schemas["HistorySessions"];
export type HistorySessionItem = Schemas["HistorySessionItem"];
export type HistorySearch = Schemas["HistorySearch"];
export type HistoryHit = Schemas["HistoryHit"];
export type TranscriptLines = Schemas["TranscriptLines"];
export type KgHealthView = Schemas["KgHealthView"];
export type KgMeasure = Schemas["KgMeasure"];
export type KgBuildCoverage = Schemas["KgBuildCoverage"];
export type ChangeReplay = Schemas["ChangeReplay"];
export type Replay = Schemas["Replay"];
export type HistorySource = HistoryHit["source"];

/** The sources the search reads, in the order its filter shows them. */
export const HISTORY_SOURCES = ["transcript", "run", "chat"] as const satisfies readonly HistorySource[];
/** Hits on a page of the search, sessions on a page of the list: the API allows up to 50 and 100. */
export const HITS_PAGE = 20;
export const SESSIONS_PAGE = 25;
/** Measures and builds the graph's health lists; the API allows up to 200. */
export const HEALTH_MEASURES = 30;
/** The clusters the History page lists; the API allows up to 200. */
export const CLUSTERS_SHOWN = 50;

export const insightKeys = {
  history: (project: string) => [...curatorKeys.all(project), "history"] as const,
  usage: (project: string) => [...curatorKeys.all(project), "transcript-usage"] as const,
  clusters: (project: string) => [...curatorKeys.all(project), "history", "clusters"] as const,
  sessions: (project: string, query: SessionsQuery) => [...curatorKeys.all(project), "history", "sessions", query] as const,
  search: (project: string, query: SearchQuery) => [...curatorKeys.all(project), "history", "search", query] as const,
  window: (project: string, session: string, file: string, line: number, before: number, after: number) =>
    ["projects", project, "transcripts", session, file, "window", line, before, after] as const,
  health: (project: string, limit: number) => [...curatorKeys.all(project), "kg-health", limit] as const,
  replay: (project: string, change: number) => [...curatorKeys.proposals(project), "replay", change] as const,
};

/** A 404 that means "nothing to show here" (no figures yet, a label the sink does not reach) reads as null. */
async function orNull<T>(read: () => Promise<T>): Promise<T | null> {
  try {
    return await read();
  } catch (error) {
    if (isApiError(error) && error.status === 404) return null;
    throw error;
  }
}

export const historyStatusQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: insightKeys.history(project),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/curator/history", { params: { path: { project } }, signal })),
    refetchInterval: CURATOR_IDLE_MS,
  });

/** What the project's transcripts come to on the hub, per member; null when its default label is beyond the sink. */
export const transcriptUsageQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: insightKeys.usage(project),
    queryFn: ({ signal }) =>
      orNull(() => call(api().GET("/v1/projects/{project}/transcripts/usage", { params: { path: { project } }, signal }))),
    refetchInterval: CURATOR_IDLE_MS,
  });

export const clustersQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: insightKeys.clusters(project),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/curator/history/clusters", {
          params: { path: { project }, query: { limit: CLUSTERS_SHOWN } },
          signal,
        }),
      ),
  });

/** The filters of GET .../curator/history/sessions the History page uses. */
export type SessionsQuery = {
  kind: "session" | "run" | null;
  outcome: string | null;
  cluster: string | null;
  offset: number;
};

export const sessionsQuery = (api: ApiSource, project: string, query: SessionsQuery) =>
  queryOptions({
    queryKey: insightKeys.sessions(project, query),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/curator/history/sessions", {
          params: {
            path: { project },
            query: {
              ...(query.kind ? { kind: query.kind } : {}),
              ...(query.outcome ? { outcome: query.outcome } : {}),
              ...(query.cluster ? { cluster: query.cluster } : {}),
              limit: SESSIONS_PAGE,
              offset: query.offset,
            },
          },
          signal,
        }),
      ),
    placeholderData: keepPreviousData,
  });

/** A search of the project's whole history: its words, the sources it reads (none: every one) and the page. */
export type SearchQuery = { q: string; sources: HistorySource[]; offset: number };

export const searchQuery = (api: ApiSource, project: string, query: SearchQuery) =>
  queryOptions({
    queryKey: insightKeys.search(project, query),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/curator/history/search", {
          params: { path: { project }, query: { q: query.q, source: query.sources, limit: HITS_PAGE, offset: query.offset } },
          signal,
        }),
      ),
    enabled: query.q.trim() !== "",
    placeholderData: keepPreviousData,
    staleTime: 60_000,
  });

/** The lines of a transcript file from `line - before` to `line + after`, each as its entries. */
export const transcriptWindowQuery = (api: ApiSource, project: string, session: string, file: string, line: number, before: number, after: number) =>
  queryOptions({
    queryKey: insightKeys.window(project, session, file, line, before, after),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/transcripts/{session_id}/lines", {
          params: { path: { project, session_id: session }, query: { line, file, before, after } },
          signal,
        }),
      ),
    placeholderData: keepPreviousData,
    staleTime: Infinity, // the lines a part holds never change
  });

/** The graph's health over time; null when the project's default label is beyond the sink. */
export const kgHealthQuery = (api: ApiSource, project: string, limit = HEALTH_MEASURES) =>
  queryOptions({
    queryKey: insightKeys.health(project, limit),
    queryFn: ({ signal }) =>
      orNull(() =>
        call(api().GET("/v1/projects/{project}/curator/kg-health", { params: { path: { project }, query: { limit } }, signal })),
      ),
    refetchInterval: CURATOR_IDLE_MS,
  });

export const changeReplayQuery = (api: ApiSource, project: string, change: number) =>
  queryOptions({
    queryKey: insightKeys.replay(project, change),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/curator/changes/{change_id}/replay", {
          params: { path: { project, change_id: change } },
          signal,
        }),
      ),
    refetchInterval: CURATOR_IDLE_MS,
  });
