import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import { isApiError } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";

import {
  BridgeError,
  type HubReader,
  MAX_ROUTE_BODY_BYTES,
  PLUGIN_HEADER,
  type RouteAnswer,
  type RouteCall,
} from "./bridge";

type RunState = components["schemas"]["Run"]["state"];

/**
 * The hub as the bridge reads it for a page of `plugin` in `project`: each method through the typed client with the
 * viewer's session cookie, so the hub decides again what the viewer may read; a route of the plugin itself through
 * /v1/x/<plugin>/<project>/<path>, with the session's CSRF header on a write. Every call names the plugin in
 * PLUGIN_HEADER, so the hub also checks it against what the project grants the plugin now. A refusal of the hub becomes
 * `hub_refused` with its status and message, no answer `hub_unavailable`.
 */
async function read<T>(pending: Promise<T>): Promise<T> {
  try {
    return await pending;
  } catch (error) {
    throw asBridgeError(error);
  }
}

function asBridgeError(error: unknown): BridgeError {
  if (error instanceof BridgeError) return error;
  if (isApiError(error) && error.kind !== "network") {
    return new BridgeError("hub_refused", error.info.message || `the hub answered ${error.info.status}`, error.info.status);
  }
  return new BridgeError("hub_unavailable", "the hub did not answer; try again shortly");
}

async function bodyOf(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text.length > MAX_ROUTE_BODY_BYTES) throw new BridgeError("too_large", "the plugin's answer is over 1 MiB");
  if ((response.headers.get("content-type") ?? "").includes("json")) {
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }
  return text;
}

export function hubReader(api: ApiClient, project: string, plugin: string): HubReader {
  const path = { project };
  const headers = { [PLUGIN_HEADER]: plugin };
  return {
    runs: ({ state, limit, offset }) => {
      const query = { state: state as RunState[] | undefined, limit, offset }; // checked against RUN_STATES
      return read(call(api.GET("/v1/projects/{project}/runs", { params: { path, query }, headers })));
    },
    run: (id) =>
      read(call(api.GET("/v1/projects/{project}/runs/{run_id}", { params: { path: { project, run_id: id } }, headers }))),
    plans: (query) => read(call(api.GET("/v1/projects/{project}/plans", { params: { path, query }, headers }))),
    plan: (id) =>
      read(
        call(api.GET("/v1/projects/{project}/plans/{plan_id}", { params: { path: { project, plan_id: id } }, headers })),
      ),
    memories: ({ limit, cursor }) =>
      read(call(api.GET("/v1/memories", { params: { query: { scope: "project", project, limit, cursor } }, headers }))),
    route: async ({ method, path: below, query, body }: RouteCall): Promise<RouteAnswer> => {
      const url = new URL(`/v1/x/${encodeURIComponent(plugin)}/${encodeURIComponent(project)}/${below}`, window.location.origin);
      for (const [name, value] of Object.entries(query)) url.searchParams.set(name, value);
      const sent: Record<string, string> = { accept: "application/json", ...headers };
      try {
        if (method !== "GET") Object.assign(sent, await csrfHeaders(api));
        if (body !== undefined) sent["content-type"] = "application/json";
        const response = await fetch(url, {
          method,
          headers: sent,
          body: body === undefined ? undefined : JSON.stringify(body),
          credentials: "same-origin",
          redirect: "error",
        });
        return { status: response.status, body: await bodyOf(response) };
      } catch (error) {
        throw asBridgeError(error);
      }
    },
  };
}
