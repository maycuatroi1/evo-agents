import type { Route } from "next";

import { filtersSearch } from "@/components/workers/model";
import { BUILD_HISTORY_ID } from "@/lib/kg/queries";

import { type AdminOverview, type ProjectFailedBuilds, searchOf, tokensHref } from "./data";

/**
 * What the admin overview shows, as pure functions of GET /v1/admin/overview: the things that may need an admin, each
 * with the page that lists them already filtered, and the areas the diagnostics page sorts the hub's tables into.
 */

export type AttentionTone = "danger" | "attention" | "neutral";

/** One thing that may need an admin, with the page that lists it filtered; null where no page lists it. */
export type Attention =
  | { kind: "builds"; tone: AttentionTone; href: Route; builds: ProjectFailedBuilds; days: number }
  | { kind: "offline"; tone: "danger"; href: Route; count: number; seconds: number }
  | { kind: "expiring"; tone: "attention"; href: Route; count: number; days: number }
  | { kind: "unused"; tone: "neutral"; href: Route; count: number; days: number }
  | { kind: "notSignedIn"; tone: "neutral"; href: Route; count: number }
  | { kind: "deletions"; tone: "neutral"; href: null; count: number; bytes: number };

/** A project's knowledge graph page with its build history showing the failed builds only. */
export function failedBuildsHref(project: string): Route {
  return `/p/${encodeURIComponent(project)}/kg?builds=failed#${BUILD_HISTORY_ID}` as Route;
}

/** The members list, of one project when given. */
export function membersHref(project?: string): Route {
  return `/admin/members${searchOf({ project: project ?? "" })}` as Route;
}

export function offlineWorkersHref(): Route {
  return `/workers${filtersSearch({ status: "offline", q: "" })}` as Route;
}

const TONE_ORDER: Record<AttentionTone, number> = { danger: 0, attention: 1, neutral: 2 };

/**
 * What may need an admin, most pressing first: projects whose newest graph build failed and offline workers (danger),
 * tokens about to expire (attention), then projects that failed lately but built since, long-unused tokens, people
 * granted access who have not signed in, and blobs still waiting to leave the bucket. A count of zero is left out.
 */
export function attentionItems(overview: AdminOverview): Attention[] {
  const { tokens, workers, members, storage, kg_builds: builds } = overview;
  const items: Attention[] = builds.projects.map((project) => ({
    kind: "builds",
    tone: project.latest_status === "failed" ? "danger" : "neutral",
    href: failedBuildsHref(project.project),
    builds: project,
    days: builds.days,
  }));
  if (workers.offline > 0) {
    items.push({ kind: "offline", tone: "danger", href: offlineWorkersHref(), count: workers.offline, seconds: workers.offline_after_seconds });
  }
  if (tokens.expiring > 0) {
    items.push({ kind: "expiring", tone: "attention", href: tokensHref({ state: "expiring" }), count: tokens.expiring, days: tokens.expiring_days });
  }
  if (tokens.unused > 0) {
    items.push({ kind: "unused", tone: "neutral", href: tokensHref({ state: "unused" }), count: tokens.unused, days: tokens.unused_days });
  }
  if (members.not_signed_in > 0) {
    items.push({ kind: "notSignedIn", tone: "neutral", href: membersHref(), count: members.not_signed_in });
  }
  if (storage.pending_deletions > 0) {
    items.push({ kind: "deletions", tone: "neutral", href: null, count: storage.pending_deletions, bytes: storage.pending_bytes });
  }
  // A stable sort: within a tone, the order above (builds latest failure first, as the API sends them).
  return items.sort((a, b) => TONE_ORDER[a.tone] - TONE_ORDER[b.tone]);
}

/** Members of a project with any role: what its row of the access list totals. */
export function grantTotal(grants: { admins: number; writers: number; readers: number }): number {
  return grants.admins + grants.writers + grants.readers;
}

// Diagnostics

export const TABLE_AREAS = [
  "access",
  "projects",
  "content",
  "runs",
  "credentials",
  "notifications",
  "graph",
  "storage",
  "queue",
  "other",
] as const;
export type TableArea = (typeof TABLE_AREAS)[number];

const EXACT: Record<string, TableArea> = {
  users: "access",
  tokens: "access",
  grants: "access",
  audit: "access",
  decisions: "runs",
  secrets: "credentials",
  secret_bindings: "credentials",
  credential_leases: "credentials",
};

/** Prefixes of table names, longest first where two overlap; a table a later revision adds lands by its prefix. */
const PREFIXES: readonly [string, TableArea][] = [
  ["project_", "projects"],
  ["projects", "projects"],
  ["memor", "content"],
  ["plan", "content"],
  ["skill", "content"],
  ["worker", "runs"],
  ["run", "runs"],
  ["notification", "notifications"],
  ["kg_", "graph"],
  ["blob", "storage"],
  ["procrastinate_", "queue"],
];

/** The area of the hub a table belongs to, from its name; `other` for one no rule knows. */
export function tableArea(table: string): TableArea {
  if (table in EXACT) return EXACT[table];
  return PREFIXES.find(([prefix]) => table.startsWith(prefix))?.[1] ?? "other";
}

export type TableRow = { table: string; area: TableArea; rows: number };

/** GET /v1/admin/stats as the diagnostics table's rows, in the API's order (by name), with their totals. */
export function tableRows(stats: Record<string, number>): { rows: TableRow[]; total: number } {
  const rows = Object.entries(stats).map(([table, count]) => ({ table, area: tableArea(table), rows: count }));
  return { rows, total: rows.reduce((sum, row) => sum + row.rows, 0) };
}
