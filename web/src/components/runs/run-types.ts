import type { components } from "@/lib/api/schema";

/** The run types of the API and the filters of the runs list; `queries.ts` exports them with what reads and writes runs. */
export type Schemas = components["schemas"];
export type Run = Schemas["Run"];
export type RunList = Schemas["RunList"];
export type RunState = Run["state"];
export type StateCounts = Schemas["StateCounts"];
export type ReadySteps = Schemas["ReadySteps"];
export type StepReadiness = Schemas["StepReadiness"];
export type DispatchRequest = Omit<Schemas["Dispatch"], "browser" | "identities"> & Partial<Pick<Schemas["Dispatch"], "browser" | "identities">>; // a browser sent only when asked for
export type PlanRunRequest = Omit<Schemas["PlanRunDispatch"], "session_per_step" | "browser" | "identities"> & { session_per_step?: true } & Partial<Pick<Schemas["PlanRunDispatch"], "browser" | "identities">>; // sent only when on

export type RunMode = DispatchRequest["mode"];
export type Approval = DispatchRequest["approval"];
export type RunKind = Run["kind"];
export type ActiveRun = Schemas["ActiveRun"];
export type PlanTimeout = PlanRunRequest["timeout_h"];

/** The filters of GET /v1/projects/{p}/runs that the pages use. */
export type RunQuery = {
  states?: readonly RunState[];
  planId?: string | null;
  step?: string | null;
  workerId?: number | null;
  /** Only runs of this kind, and only local runs of this Claude account (email). */
  kind?: RunKind | null;
  account?: string | null;
  /** The turns of one chat session (`session_run_id`); `group: "session"` lists each chat session once, its latest turn. */
  sessionRunId?: number | null; group?: "session" | null;
  q?: string;
  limit?: number;
  offset?: number;
};

export type RunEvent = Schemas["RunEvent"];
export type RunEventKind = RunEvent["kind"];
export type RunEventPage = Schemas["RunEvents"];
export type RunMessage = Schemas["Message"];
export type DiffLink = Schemas["DiffLink"];

export type RunLease = Schemas["RunLease"];

export type RunControl = "cancel" | "takeover" | "handback" | "approve" | "rerun";

export type AuthorRunRequest = Schemas["AuthorRunDispatch"];
export type AuthorTimeout = AuthorRunRequest["timeout_h"];
export type RunChat = Schemas["RunChat"];
export type ChatMessage = Schemas["ChatMessage"];
export type ChatStatus = RunChat["status"];
