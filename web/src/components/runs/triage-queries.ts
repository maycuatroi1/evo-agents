import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

type Schemas = components["schemas"];

/** Who can run a failed run again, and where the hub opened its triage thread (docs/workers.md, Failure triage). */
export type RerunCandidates = Schemas["RerunCandidates"];
export type WorkerCandidate = Schemas["WorkerCandidate"];
export type AgentCandidate = Schemas["AgentCandidate"];
export type AccountCandidate = Schemas["AccountCandidate"];
export type OtherProject = Schemas["OtherProject"];

/** The kinds of run whose failure opens a triage thread (`triage.KINDS` of the hub). */
export const TRIAGE_KINDS: readonly string[] = ["step", "plan", "author"];

/** The causes the hub names (`runs.FAILURE_CAUSES`), each with a line of its own in the messages. */
export const KNOWN_CAUSES = [
  "origin",
  "credentials",
  "missing_tool",
  "push_conflict",
  "verify_failed",
  "timeout",
  "cost_cap",
  "turn_cap",
  "time_cap",
  "checkout",
  "runtime",
  "worker_stopped",
  "branch_mismatch",
  "rate_limit",
  "account_auth",
] as const;

export type KnownCause = (typeof KNOWN_CAUSES)[number];

/** The cause as a key of the messages: the ones the hub names, else "other". */
export const causeKey = (cause: string): KnownCause | "other" => ((KNOWN_CAUSES as readonly string[]).includes(cause) ? (cause as KnownCause) : "other");

export const runCandidatesQuery = (api: ApiSource, project: string, runId: number) =>
  queryOptions({
    queryKey: ["projects", project, "runs", runId, "rerun-candidates"] as const,
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/runs/{run_id}/rerun-candidates", {
          params: { path: { project, run_id: runId } },
          signal,
        }),
      ),
    // The picture of a failed run does not move: workers come and go, and a reload reads it again.
    staleTime: 30_000,
    retry: false,
  });

/** The web page of the thread, as the hub links it (`chats.chat_link`): /chats/ID?message=ID. */
export const triageHref = (link: string): Route => link as Route;
