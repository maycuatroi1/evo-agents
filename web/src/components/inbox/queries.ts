import { keepPreviousData, queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { decisionHref } from "@/components/runs/queries";
import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import { isApiError } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * What the Inbox reads and writes (docs/notifications.md): the member's own notifications and their unread count, the
 * decisions the agents of plan runs ask, and the answers of a run's owner. A member reads only their own notifications;
 * any reader of a plan reads its decisions; only the run's owner answers one.
 */
type Schemas = components["schemas"];
export type Notification = Schemas["Notification"];
export type NotificationList = Schemas["NotificationList"];
export type NotificationCount = Schemas["NotificationCount"];
export type NotificationKind = Notification["kind"];
export type NoticeKind = NonNullable<Notification["notice_kind"]>;
export type Decision = Schemas["Decision"];
export type DecisionState = Decision["state"];
export type DecisionCategory = Decision["category"];
export type DecisionOption = Schemas["DecisionOption"];
export type AnswerRequest = Schemas["AnswerIn"];

/** `runs.NOTIFICATION_KINDS`, `runs.NOTICE_KINDS`, `runs.DECISION_STATES` and `runs.DECISION_CATEGORIES`, in order. */
export const NOTIFICATION_KINDS = ["decision", "notice", "proposal"] as const satisfies readonly NotificationKind[];
export const NOTICE_KINDS = [
  "push_default_branch",
  "merge_default_branch",
  "plan_finished",
  "run_failed",
  "curator_brief",
  "curator_paused",
  "author_waiting",
] as const satisfies readonly NoticeKind[];
export const DECISION_STATES = ["open", "answered", "expired", "cancelled"] as const satisfies readonly DecisionState[];
export const DECISION_CATEGORIES = [
  "deploy",
  "delete_data",
  "live_migration",
  "external_send",
  "spend_money",
  "architecture",
  "scope",
] as const satisfies readonly DecisionCategory[];

/** `runs.MAX_ANSWER_BYTES`: the owner's own words in an answer are at most 4 KiB of UTF-8. */
export const MAX_ANSWER_BYTES = 4 * 1024;
/** The bell and the Inbox ask the hub every 10 seconds. */
export const INBOX_REFRESH_MS = 10_000;
/** Notifications on one page of the Inbox; the API allows up to 200. */
export const INBOX_PAGE = 50;
/** `MAX_READ_IDS` of the API: the most notifications one request marks read by id. */
export const MAX_READ_IDS = 500;

export const INBOX_HREF = "/inbox" as Route;

export { decisionHref };

/** The filters of GET /v1/me/notifications that the Inbox uses. */
export type InboxQuery = {
  kind: NotificationKind | null;
  project: string | null;
  unread: boolean;
  limit: number;
  offset: number;
};

export const inboxKeys = {
  /** Every read of the member's notifications: invalidated after a mark read or an answer. */
  all: ["me", "notifications"] as const,
  count: ["me", "notifications", "count"] as const,
  list: (query: InboxQuery) => ["me", "notifications", "list", query] as const,
  /** Where a decision opened from a link lives, when the Inbox's list does not say. */
  located: (id: number) => ["me", "decision-located", id] as const,
};

/** Below the project's decisions (`["projects", p, "decisions"]`), which the run pages read too. */
export function decisionKeys(project: string) {
  return {
    all: ["projects", project, "decisions"] as const,
    one: (id: number) => ["projects", project, "decisions", "one", id] as const,
  };
}

/** The unread notifications and the decisions waiting for the member's answer, for the bell: asked every 10 seconds. */
export const notificationCountQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: inboxKeys.count,
    queryFn: ({ signal }) => call(api().GET("/v1/me/notifications/count", { signal })),
    refetchInterval: INBOX_REFRESH_MS,
  });

/** One page of the member's notifications, open decisions first, then newest first. */
export const notificationsQuery = (api: ApiSource, query: InboxQuery) =>
  queryOptions({
    queryKey: inboxKeys.list(query),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/me/notifications", {
          params: {
            query: {
              ...(query.kind ? { kind: query.kind } : {}),
              ...(query.project ? { project: query.project } : {}),
              ...(query.unread ? { unread: true } : {}),
              limit: query.limit,
              offset: query.offset,
            },
          },
          signal,
        }),
      ),
    placeholderData: keepPreviousData, // the last page stays on screen while the next filter or page loads
    refetchInterval: INBOX_REFRESH_MS,
  });

/**
 * One decision; asked again every 10 seconds while it is open (its owner may answer it from the command line) and,
 * once answered, until the worker has handed the answer to the agent.
 */
export const decisionQuery = (api: ApiSource, project: string, id: number) =>
  queryOptions({
    queryKey: decisionKeys(project).one(id),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/decisions/{decision_id}", {
          params: { path: { project, decision_id: id } },
          signal,
        }),
      ),
    refetchInterval: (query) => {
      const decision = query.state.data;
      const moving = decision?.state === "open" || (decision?.state === "answered" && decision.delivered_at === null);
      return moving ? INBOX_REFRESH_MS : false;
    },
  });

export type LocatedDecision = { project: string; decision: Decision };

/**
 * A decision opened from a link (`/inbox?decision=ID`) that names no project: asked of each project the member holds a
 * grant on, at once. A project that does not hold it answers 404 (403 where the plan is hidden); null when none does.
 * Any other failure is thrown when no project answered, so the page offers a retry.
 */
export const locateDecisionQuery = (api: ApiSource, id: number, projects: readonly string[]) =>
  queryOptions({
    queryKey: [...inboxKeys.located(id), [...projects].sort()] as const,
    queryFn: async ({ signal }): Promise<LocatedDecision | null> => {
      const answers = await Promise.allSettled(
        projects.map(async (project) => ({
          project,
          decision: await call(
            api().GET("/v1/projects/{project}/decisions/{decision_id}", {
              params: { path: { project, decision_id: id } },
              signal,
            }),
          ),
        })),
      );
      const found = answers.find((answer) => answer.status === "fulfilled");
      if (found && found.status === "fulfilled") return found.value;
      const failure = answers.find(
        (answer) => answer.status === "rejected" && !(isApiError(answer.reason) && (answer.reason.status === 404 || answer.reason.status === 403)),
      );
      if (failure && failure.status === "rejected") throw failure.reason;
      return null;
    },
    staleTime: Infinity, // a decision never moves to another project
  });

/** Mark the member's notifications read, those named or all of them, with the session's CSRF header. */
export async function markNotificationsRead(api: ApiClient, target: { ids: number[] } | { all: true }) {
  const headers = await csrfHeaders(api);
  const body = "all" in target ? { all: true } : { ids: target.ids, all: false };
  return call(api.POST("/v1/me/notifications/read", { body, headers }));
}

/** Answer a decision of a run the member dispatched: an option, words of their own, or both. */
export async function answerDecision(api: ApiClient, project: string, id: number, body: AnswerRequest): Promise<Decision> {
  const headers = await csrfHeaders(api);
  return call(
    api.POST("/v1/projects/{project}/decisions/{decision_id}/answer", {
      params: { path: { project, decision_id: id } },
      body,
      headers,
    }),
  );
}
