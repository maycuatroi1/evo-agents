import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * The reads of a project's signals (docs/signals.md; evo_agents.hub.server.signals and signal_keys): the keys of its
 * registry and the signals of one key in a span of time, which the Signals page draws and a canvas's claim names in
 * its test card and links. Both feature folders read them (components/signals, components/canvas), so they live here.
 */
type Schemas = components["schemas"];
export type SignalKey = Schemas["SignalKey"];
export type Signal = Schemas["Signal"];
export type SignalKind = SignalKey["kind"];

/** `signals.MAX_LIMIT`: the most signals one read returns, the newest first. */
export const MAX_SIGNALS = 1_000;
/** `tables_signals.MAX_TENANT_CHARS`. */
export const MAX_TENANT_CHARS = 100;
/** A key as the registry names one: two to six dotted parts (`tables_signals.KEY_NAME`). */
export const SIGNAL_KEY_NAME = /^[a-z][a-z0-9_]*(\.[a-z0-9_]+){1,5}$/;

export type SignalsFilter = {
  key: string | null;
  tenant: string | null;
  /** Measured at or after, an ISO time; null for every time the hub keeps. */
  since: string | null;
  limit: number;
};

export const signalKeys = {
  all: (project: string) => ["projects", project, "signals"] as const,
  keys: (project: string) => [...signalKeys.all(project), "keys"] as const,
  list: (project: string, filter: SignalsFilter) => [...signalKeys.all(project), "list", filter] as const,
};

/** The keys of the project's registry that take signals now, by name. */
export const signalKeysQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: signalKeys.keys(project),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/signal-keys", { params: { path: { project } }, signal })),
  });

/** The signals the member may read, newest first: of one key or all, of one tenant, measured since a time. */
export const signalsQuery = (api: ApiSource, project: string, filter: SignalsFilter) =>
  queryOptions({
    queryKey: signalKeys.list(project, filter),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/signals", {
          params: {
            path: { project },
            query: {
              limit: filter.limit,
              ...(filter.key ? { key: filter.key } : {}),
              ...(filter.tenant ? { tenant: filter.tenant } : {}),
              ...(filter.since ? { since: filter.since } : {}),
            },
          },
          signal,
        }),
      ),
  });

/**
 * The project's Signals page, on one key and one tenant when given: the page the value check links from the evidence
 * it writes (evo_agents.hub.server.canvas.checks.link), `/p/<project>/signals?key=...&tenant=...`.
 */
export function signalsHref(project: string, key: string | null = null, tenant: string | null = null): Route {
  const search = new URLSearchParams();
  if (key) search.set("key", key);
  if (tenant) search.set("tenant", tenant);
  const query = search.toString();
  return `/p/${encodeURIComponent(project)}/signals${query ? `?${query}` : ""}` as Route;
}
