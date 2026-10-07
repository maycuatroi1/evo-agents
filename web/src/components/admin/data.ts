import { keepPreviousData, queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import { type ApiSource, PROJECT_NAME } from "@/lib/queries";

/**
 * What the admin area reads and writes, shared by its server pages (prefetch) and client components. Filters live in
 * the URL, so a filtered view can be shared and survives a reload; each page parses them the same way on the server
 * and in the browser, which keeps the query keys equal and lets prefetched data hydrate.
 */

type Schemas = components["schemas"];
export type AdminUser = Schemas["UserRow"];
export type UserGrant = Schemas["UserGrant"];
export type AuditRow = Schemas["AuditRow"];
export type AuditPage = Schemas["AuditPage"];
export type AdminToken = Schemas["AdminToken"];
export type TokenPage = Schemas["TokenPage"];
export type AdminOverview = Schemas["AdminOverview"];
export type ProjectFailedBuilds = Schemas["ProjectFailedBuilds"];
export type ProjectGrantCounts = Schemas["ProjectGrantCounts"];
export type Role = "reader" | "writer" | "admin";
export const ROLES: readonly Role[] = ["reader", "writer", "admin"];

/** `admin.LOGIN_NAME`, `admin_console.ACTION_NAME`: what the API accepts. */
export const LOGIN_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/;
export const ACTION_NAME = /^[a-z][a-z0-9_.-]{0,99}$/;
const CURSOR = /^[A-Za-z0-9_-]{1,200}$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;

export const PAGE_SIZES = [25, 50, 100] as const;
export type PageSize = (typeof PAGE_SIZES)[number];
export const DEFAULT_PAGE_SIZE: PageSize = 50;

export const TOKEN_KINDS = ["machine", "web", "worker"] as const;
export type TokenKind = (typeof TOKEN_KINDS)[number];
/**
 * The token list's state filter. Besides a token's own state: `expiring`, live ones that expire within
 * `TokenCounts.expiring_days` (14), and `unused`, those not revoked and unused for `unused_days` (90), as the admin
 * overview counts them; the overview's links open the list on these.
 */
export const TOKEN_STATES = ["active", "expiring", "unused", "revoked", "expired", "any"] as const;
export type TokenStateFilter = (typeof TOKEN_STATES)[number];
/** The windows of the `expiring` and `unused` filters, as the API's TOKEN_EXPIRING_DAYS and TOKEN_UNUSED_DAYS. */
export const TOKEN_FILTER_DAYS = { expiring: 14, unused: 90 } as const;

/** A URLSearchParams, or the `searchParams` record a server page receives. */
export type ParamSource = { get(name: string): string | null };

export function fromRecord(record: Record<string, string | string[] | undefined>): ParamSource {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(record)) {
    const first = Array.isArray(value) ? value[0] : value;
    if (first !== undefined) params.set(key, first);
  }
  return params;
}

function pick(source: ParamSource, name: string, pattern: RegExp): string {
  const value = source.get(name)?.trim() ?? "";
  return pattern.test(value) ? value : "";
}

function pageSize(source: ParamSource): PageSize {
  const value = Number(source.get("limit"));
  return PAGE_SIZES.find((size) => size === value) ?? DEFAULT_PAGE_SIZE;
}

function isDate(value: string): boolean {
  if (!DATE.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
}

/** The URL of a view: its non-empty values, in a stable order. */
export function searchOf(values: Record<string, string | number>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value !== "" && value !== undefined) params.set(key, String(value));
  const text = params.toString();
  return text ? `?${text}` : "";
}

// Time

/** Milliseconds `timeZone` is ahead of UTC at `instant`. */
function offsetAt(instant: number, timeZone: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(new Date(instant));
  const part = (type: Intl.DateTimeFormatPartTypes) => Number(parts.find((p) => p.type === type)?.value ?? 0);
  const local = Date.UTC(part("year"), part("month") - 1, part("day"), part("hour"), part("minute"), part("second"));
  return local - Math.floor(instant / 1000) * 1000;
}

/** The instant a calendar day (YYYY-MM-DD) starts in `timeZone`, as ISO 8601 in UTC. */
export function startOfDay(date: string, timeZone: string): string {
  const [year, month, day] = date.split("-").map(Number);
  const wall = Date.UTC(year, month - 1, day);
  const first = wall - offsetAt(wall, timeZone);
  const settled = wall - offsetAt(first, timeZone); // the offset at midnight itself, across a DST change
  return new Date(settled).toISOString();
}

/** The calendar day after `date` (YYYY-MM-DD). */
export function nextDay(date: string): string {
  const [year, month, day] = date.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day + 1)).toISOString().slice(0, 10);
}

// Users and projects

export const adminKeys = {
  all: ["admin"] as const,
  users: ["admin", "users"] as const,
  auditAll: ["admin", "audit"] as const,
  audit: (params: AuditParams) => ["admin", "audit", "page", params] as const,
  auditActions: ["admin", "audit", "actions"] as const,
  tokensAll: ["admin", "tokens"] as const,
  tokens: (params: TokenParams) => ["admin", "tokens", "page", params] as const,
  overview: ["admin", "overview"] as const,
  stats: ["admin", "stats"] as const,
};

/** GET /v1/admin/overview: what may need an admin, in one read. Admin writes invalidate it with the rest of `admin`. */
export const adminOverviewQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: adminKeys.overview,
    queryFn: ({ signal }) => call(api().GET("/v1/admin/overview", { signal })),
  });

/** GET /v1/admin/stats: the rows of every hub table, for the diagnostics page. */
export const adminStatsQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: adminKeys.stats,
    queryFn: ({ signal }) => call(api().GET("/v1/admin/stats", { signal })),
  });

export const adminUsersQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: adminKeys.users,
    queryFn: ({ signal }) => call(api().GET("/v1/admin/users", { signal })),
  });

/** What a member's page asks the API for besides the user list: their live tokens and their latest actions. */
export const memberTokensParams = (login: string): TokenParams => ({ login, state: "active", limit: 50 });
export const memberAuditParams = (login: string): AuditParams => ({ actor: login, limit: 10 });

export function memberHref(login: string): Route {
  return `/admin/members/${encodeURIComponent(login)}` as Route;
}

export type MemberFilters = { q: string; project: string };

export function parseMemberFilters(source: ParamSource): MemberFilters {
  const q = (source.get("q") ?? "").trim().slice(0, 100);
  return { q, project: pick(source, "project", PROJECT_NAME) };
}

export function filterMembers(users: AdminUser[], filters: MemberFilters): AdminUser[] {
  const needle = filters.q.toLowerCase();
  return users.filter(
    (user) =>
      (!needle || user.login.toLowerCase().includes(needle)) &&
      (!filters.project || user.grants.some((grant) => grant.project === filters.project)),
  );
}

// The audit trail

export type AuditFilters = {
  actor: string;
  action: string;
  project: string;
  /** First day shown, YYYY-MM-DD in the hub's display time zone. */
  from: string;
  /** Last day shown, inclusive. */
  to: string;
  limit: PageSize;
  cursor: string;
};

export type AuditParams = {
  actor?: string;
  action?: string;
  project?: string;
  since?: string;
  until?: string;
  cursor?: string;
  limit: number;
};

export function parseAuditFilters(source: ParamSource): AuditFilters {
  let from = pick(source, "from", DATE);
  let to = pick(source, "to", DATE);
  if (from && !isDate(from)) from = "";
  if (to && !isDate(to)) to = "";
  if (from && to && from > to) [from, to] = [to, from];
  return {
    actor: pick(source, "actor", LOGIN_NAME),
    action: pick(source, "action", ACTION_NAME),
    project: pick(source, "project", PROJECT_NAME),
    from,
    to,
    limit: pageSize(source),
    cursor: pick(source, "cursor", CURSOR),
  };
}

/** The API's query for a view: whole days in `timeZone`, the last one included. */
export function auditParams(filters: AuditFilters, timeZone: string): AuditParams {
  const params: AuditParams = { limit: filters.limit };
  if (filters.actor) params.actor = filters.actor;
  if (filters.action) params.action = filters.action;
  if (filters.project) params.project = filters.project;
  if (filters.from) params.since = startOfDay(filters.from, timeZone);
  if (filters.to) params.until = startOfDay(nextDay(filters.to), timeZone);
  if (filters.cursor) params.cursor = filters.cursor;
  return params;
}

export const auditQuery = (api: ApiSource, params: AuditParams) =>
  queryOptions({
    queryKey: adminKeys.audit(params),
    queryFn: ({ signal }) => call(api().GET("/v1/admin/audit", { params: { query: params }, signal })),
    placeholderData: keepPreviousData,
  });

export const auditActionsQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: adminKeys.auditActions,
    queryFn: ({ signal }) => call(api().GET("/v1/admin/audit/actions", { signal })),
  });

/** A view's URL values: defaults left out, so the plain page has the plain URL. */
export function auditSearch(filters: Partial<AuditFilters>): Record<string, string | number> {
  const { limit, ...rest } = filters;
  return { ...rest, limit: limit === undefined || limit === DEFAULT_PAGE_SIZE ? "" : limit };
}

export function auditHref(filters: Partial<AuditFilters>): Route {
  return `/admin/audit${searchOf(auditSearch(filters))}` as Route;
}

// Tokens

export type TokenFilters = {
  login: string;
  kind: TokenKind | "";
  state: TokenStateFilter;
  limit: PageSize;
  cursor: string;
};

export type TokenParams = {
  login?: string;
  kind?: TokenKind;
  state: TokenStateFilter;
  cursor?: string;
  limit: number;
};

export function parseTokenFilters(source: ParamSource): TokenFilters {
  const kind = source.get("kind");
  const state = source.get("state");
  return {
    login: pick(source, "login", LOGIN_NAME),
    kind: TOKEN_KINDS.find((value) => value === kind) ?? "",
    state: TOKEN_STATES.find((value) => value === state) ?? "active",
    limit: pageSize(source),
    cursor: pick(source, "cursor", CURSOR),
  };
}

export function tokenParams(filters: TokenFilters): TokenParams {
  const params: TokenParams = { state: filters.state, limit: filters.limit };
  if (filters.login) params.login = filters.login;
  if (filters.kind) params.kind = filters.kind;
  if (filters.cursor) params.cursor = filters.cursor;
  return params;
}

export const tokensQuery = (api: ApiSource, params: TokenParams) =>
  queryOptions({
    queryKey: adminKeys.tokens(params),
    queryFn: ({ signal }) => call(api().GET("/v1/admin/tokens", { params: { query: params }, signal })),
    placeholderData: keepPreviousData,
  });

export function tokenSearch(filters: Partial<TokenFilters>): Record<string, string | number> {
  const { limit, state, ...rest } = filters;
  return {
    ...rest,
    state: state === undefined || state === "active" ? "" : state,
    limit: limit === undefined || limit === DEFAULT_PAGE_SIZE ? "" : limit,
  };
}

export function tokensHref(filters: Partial<TokenFilters>): Route {
  return `/admin/tokens${searchOf(tokenSearch(filters))}` as Route;
}

// Writes. Each one made with the session cookie carries the session's X-Evo-CSRF header, fetched just before it.

export type GrantInput = { login: string; project: string; role: Role; maxLevel: string };

export async function putGrant(api: ApiClient, input: GrantInput) {
  const headers = await csrfHeaders(api);
  return call(
    api.PUT("/v1/admin/projects/{project}/grants/{login}", {
      params: { path: { project: input.project, login: input.login } },
      body: { role: input.role, max_level: input.maxLevel },
      headers,
    }),
  );
}

export async function deleteGrant(api: ApiClient, project: string, login: string): Promise<void> {
  const headers = await csrfHeaders(api);
  await call(api.DELETE("/v1/admin/projects/{project}/grants/{login}", { params: { path: { project, login } }, headers }));
}

export async function revokeToken(api: ApiClient, tokenId: number): Promise<void> {
  const headers = await csrfHeaders(api);
  await call(api.DELETE("/v1/admin/tokens/{token_id}", { params: { path: { token_id: tokenId } }, headers }));
}

/** Audit actions the hub writes today, by message key; anything else shows its raw name. */
export function actionKey(action: string): string {
  return action.replaceAll(".", "_");
}
