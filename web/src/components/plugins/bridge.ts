/**
 * The bridge evo-plugin/1 between an isolated plugin's page and the hub (docs/plugins.md, Pages): the page, framed in
 * a sandbox with an opaque origin, posts requests to the web, and the web answers each one by calling the hub with the
 * viewer's own session, so the viewer's grant applies on top of the plugin's. A method opens only when the project
 * grants the plugin the permission it needs (METHODS) now: the web reads the plugin's grant again before such a call
 * (`Host.granted`), and the rest are refused with `not_granted` and never reach the hub. Each call names the plugin in
 * the header X-Evo-Plugin (PLUGIN_HEADER), and the hub checks it again against what the plugin may do in the project
 * as it arrives (evo_agents.hub.server.plugin_bridge), so a page left open after its grant changed gets nothing more.
 * This module is the protocol without the DOM: what a message must be, which methods a grant opens, and what each one
 * asks of the hub (`HubReader`, which plugin-frame.tsx gives it).
 *
 * A request is `{bridge: "evo-plugin/1", id, method, params}`; the answer `{bridge, id, result}` or
 * `{bridge, id, error: {code, message, status?}}`. The web also posts `{bridge, event: "theme", theme}` when the
 * viewer's theme changes.
 */

export const BRIDGE = "evo-plugin/1";
export const PLUGIN_HEADER = "X-Evo-Plugin"; // names the plugin of every call the bridge makes to the hub
export const MAX_PARAMS_BYTES = 64 * 1024; // what one request may carry, as JSON
export const MAX_ROUTE_BODY_BYTES = 1024 * 1024;
export const MAX_IN_FLIGHT = 16; // requests of one page answered at once; more wait for none and are refused
export const FRAME_HEIGHT = { min: 240, max: 4000, initial: 560 };

export type ErrorCode =
  | "unsupported_bridge"
  | "invalid_request"
  | "unknown_method"
  | "not_granted"
  | "invalid_params"
  | "too_large"
  | "busy"
  | "hub_refused"
  | "hub_unavailable";

export class BridgeError extends Error {
  constructor(
    readonly code: ErrorCode,
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "BridgeError";
  }
}

export type Theme = "light" | "dark";

export type BridgeContext = {
  bridge: typeof BRIDGE;
  project: string;
  plugin: string;
  page: string;
  viewer: { login: string; role: string | null };
  locale: string;
  theme: Theme;
  methods: string[];
};

export type RouteMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
export type RouteCall = { method: RouteMethod; path: string; query: Record<string, string>; body?: unknown };
export type RouteAnswer = { status: number; body: unknown };

/** What the methods ask of the hub, with the viewer's session; each rejects with the hub's refusal. */
export type HubReader = {
  runs: (query: { state?: string[]; limit: number; offset: number }) => Promise<unknown>;
  run: (id: number) => Promise<unknown>;
  plans: (query: { area?: "active" | "completed" }) => Promise<unknown>;
  plan: (id: string) => Promise<unknown>;
  memories: (query: { limit: number; cursor?: string }) => Promise<unknown>;
  route: (call: RouteCall) => Promise<RouteAnswer>;
};

export type Host = {
  context: () => BridgeContext;
  resize: (height: number) => void;
  /** What the project grants the plugin now, read again from the hub; nothing once it is not on there. */
  granted: () => Promise<readonly string[]>;
  hub: HubReader;
};

export type BridgeRequest = { id: string | number; method: string; params: Record<string, unknown> };

/** The methods of evo-plugin/1 and the permission each needs; null for those every page has. */
export const METHODS = {
  context: null,
  resize: null,
  route: null,
  "runs.list": "runs:read",
  "runs.get": "runs:read",
  "plans.list": "plans:read",
  "plans.get": "plans:read",
  "memories.list": "memories:read",
} as const satisfies Record<string, string | null>;
export type Method = keyof typeof METHODS;

const RUN_STATES = new Set([
  ...["queued", "leased", "running", "interactive", "verifying", "waiting"],
  ...["review", "parked", "done", "failed", "lost", "cancelled"],
]);
const ROUTE_METHODS = new Set<string>(["GET", "POST", "PUT", "PATCH", "DELETE"]);
const ROUTE_PATH = /^[A-Za-z0-9._~!$&'()*+,;=:@-]+(\/[A-Za-z0-9._~!$&'()*+,;=:@-]+)*\/?$/;
const PLAN_ID = /^[a-z0-9][a-z0-9-]{0,99}$/;

function isMethod(name: string): name is Method {
  return Object.prototype.hasOwnProperty.call(METHODS, name);
}

/** The methods a page whose plugin holds `granted` may call, in METHODS' order. */
export function allowedMethods(granted: readonly string[]): Method[] {
  return (Object.keys(METHODS) as Method[]).filter((name) => {
    const needs = METHODS[name];
    return needs === null || granted.includes(needs);
  });
}

function bytesOf(value: unknown): number {
  try {
    return new TextEncoder().encode(JSON.stringify(value) ?? "").length;
  } catch {
    return Number.POSITIVE_INFINITY; // a cycle, a BigInt: nothing the hub could take
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * The request a message holds; null for a message that is not one of the bridge's (it is ignored), a BridgeError for
 * one that is but cannot be answered as asked, with the id to answer it under when it has one.
 */
export function readRequest(data: unknown): BridgeRequest | null | { id: string | number | null; error: BridgeError } {
  if (!isRecord(data) || typeof data.bridge !== "string" || !data.bridge.startsWith("evo-plugin/")) return null;
  const id = typeof data.id === "string" || typeof data.id === "number" ? data.id : null;
  if (data.bridge !== BRIDGE) {
    return { id, error: new BridgeError("unsupported_bridge", `this hub speaks ${BRIDGE}, not ${data.bridge}`) };
  }
  if (id === null || (typeof id === "string" && id.length > 100)) {
    return { id: null, error: new BridgeError("invalid_request", "a request needs an id, a string or a number") };
  }
  if (typeof data.method !== "string") {
    return { id, error: new BridgeError("invalid_request", "a request names its method") };
  }
  const params = data.params ?? {};
  if (!isRecord(params)) return { id, error: new BridgeError("invalid_params", "params is an object") };
  if (bytesOf(params) > MAX_PARAMS_BYTES) {
    return { id, error: new BridgeError("too_large", `params are at most ${MAX_PARAMS_BYTES / 1024} KiB as JSON`) };
  }
  return { id, method: data.method, params };
}

function intParam(params: Record<string, unknown>, key: string, fallback: number, min: number, max: number): number {
  const value = params[key];
  if (value === undefined) return fallback;
  if (typeof value !== "number" || !Number.isInteger(value) || value < min || value > max) {
    throw new BridgeError("invalid_params", `${key} is a whole number from ${min} to ${max}`);
  }
  return value;
}

function stringList(value: unknown, key: string, allowed: Set<string>): string[] | undefined {
  if (value === undefined) return undefined;
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string" || !allowed.has(item))) {
    throw new BridgeError("invalid_params", `${key} lists states of runs, such as running or done`);
  }
  return value as string[];
}

function routeCall(params: Record<string, unknown>): RouteCall {
  const method = params.method ?? "GET";
  if (typeof method !== "string" || !ROUTE_METHODS.has(method)) {
    throw new BridgeError("invalid_params", "method is GET, POST, PUT, PATCH or DELETE");
  }
  const path = params.path;
  if (typeof path !== "string" || !ROUTE_PATH.test(path) || path.split("/").some((part) => part === "." || part === "..")) {
    throw new BridgeError("invalid_params", "path is a path below the plugin's routes, such as hello or items/12");
  }
  const query = params.query ?? {};
  if (!isRecord(query) || Object.values(query).some((value) => typeof value !== "string")) {
    throw new BridgeError("invalid_params", "query maps names to strings");
  }
  if (params.body !== undefined && method === "GET") {
    throw new BridgeError("invalid_params", "a GET carries no body");
  }
  if (bytesOf(params.body) > MAX_ROUTE_BODY_BYTES) {
    throw new BridgeError("too_large", `a body is at most ${MAX_ROUTE_BODY_BYTES / 1024 / 1024} MiB as JSON`);
  }
  return { method: method as RouteMethod, path, query: query as Record<string, string>, body: params.body };
}

/**
 * The answer to `request` from a page of the plugin `host` serves; rejects with a BridgeError. A method that needs a
 * permission asks `host.granted()` first, so it goes by the grant of now, not by the one the page opened with.
 */
export async function answer(request: BridgeRequest, host: Host): Promise<unknown> {
  const { method, params } = request;
  if (!isMethod(method)) throw new BridgeError("unknown_method", `${BRIDGE} has no method ${method}`);
  const needs = METHODS[method];
  if (needs !== null && !(await host.granted()).includes(needs)) {
    const project = host.context().project;
    throw new BridgeError("not_granted", `${method} needs ${needs}, which project ${project} did not grant this plugin`);
  }
  switch (method) {
    case "context":
      return host.context();
    case "resize": {
      const height = params.height;
      if (typeof height !== "number" || !Number.isFinite(height)) {
        throw new BridgeError("invalid_params", "height is the page's height in CSS pixels");
      }
      const clamped = Math.round(Math.min(FRAME_HEIGHT.max, Math.max(FRAME_HEIGHT.min, height)));
      host.resize(clamped);
      return { height: clamped };
    }
    case "route":
      return host.hub.route(routeCall(params));
    case "runs.list":
      return host.hub.runs({
        state: stringList(params.state, "state", RUN_STATES),
        limit: intParam(params, "limit", 20, 1, 50),
        offset: intParam(params, "offset", 0, 0, 100_000),
      });
    case "runs.get":
      if (params.id === undefined) throw new BridgeError("invalid_params", "id is the run's number");
      return host.hub.run(intParam(params, "id", 0, 1, Number.MAX_SAFE_INTEGER));
    case "plans.list": {
      const area = params.area;
      if (area !== undefined && area !== "active" && area !== "completed") {
        throw new BridgeError("invalid_params", "area is active or completed");
      }
      return host.hub.plans({ area });
    }
    case "plans.get": {
      if (typeof params.id !== "string" || !PLAN_ID.test(params.id)) {
        throw new BridgeError("invalid_params", "id is a plan's id");
      }
      return host.hub.plan(params.id);
    }
    case "memories.list": {
      const cursor = params.cursor;
      if (cursor !== undefined && typeof cursor !== "string") throw new BridgeError("invalid_params", "cursor is a string");
      return host.hub.memories({ limit: intParam(params, "limit", 20, 1, 100), cursor });
    }
  }
}

/** The message that answers request `id`: its result, or the error it failed with. */
export function reply(id: string | number | null, outcome: { result: unknown } | { error: unknown }) {
  if ("result" in outcome) return { bridge: BRIDGE, id, result: outcome.result ?? null };
  const error = outcome.error;
  if (error instanceof BridgeError) {
    return { bridge: BRIDGE, id, error: { code: error.code, message: error.message, status: error.status } };
  }
  return { bridge: BRIDGE, id, error: { code: "hub_unavailable", message: "the hub did not answer" } };
}
