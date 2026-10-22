import type { Route } from "next";

import { LEVELS, type Level, type OrgChart, type OrgRuns, type OrgSeat, type OrgUnit, type OrgUser, type OrgViewer, ROLES, type SeatRole } from "./queries";

/**
 * How the Org page reads the chart: the tree of units, which seats ended or end soon, the runs of each person, the
 * people a hub admin has not placed yet, and which writes the viewer may make. The rights mirror `evo_agents.hub.org`
 * (a hub admin changes everything; a lead manages the units below their own, the member and intern seats of their own
 * unit and every seat below it, never above their own level): the page hides what the hub would refuse, and the hub
 * still decides. Pure functions, shared by the page and its tests.
 */

/** How many days ahead a seat's end shows as a warning. */
export const ENDS_SOON_DAYS = 7;
const DAY_MS = 86_400_000;

/** The day of `now` in UTC, as the hub's dates read: YYYY-MM-DD. */
export function utcDay(now: number): string {
  return new Date(now).toISOString().slice(0, 10);
}

function daysBetween(from: string, to: string): number {
  return Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / DAY_MS);
}

export function levelRank(level: Level): number {
  return LEVELS.indexOf(level);
}

/** The root: the one unit without a parent. */
export function rootOf(units: readonly OrgUnit[]): OrgUnit | undefined {
  return units.find((unit) => unit.parent === null);
}

/** The unit `slug` and every unit above it, up to the root. */
export function ancestors(units: readonly OrgUnit[], slug: string): OrgUnit[] {
  const bySlug = new Map(units.map((unit) => [unit.slug, unit]));
  const chain: OrgUnit[] = [];
  let current = bySlug.get(slug);
  while (current && !chain.includes(current)) {
    chain.push(current);
    current = current.parent === null ? undefined : bySlug.get(current.parent);
  }
  return chain;
}

/** Whether unit `slug` lies strictly below unit `above`. */
export function isBelow(units: readonly OrgUnit[], slug: string, above: string): boolean {
  return ancestors(units, slug).some((unit) => unit.slug === above && unit.slug !== slug);
}

/** The day a seat ends: the earliest of its own end date and those of the cohorts holding it; null for none. */
export function endDay(units: readonly OrgUnit[], seat: OrgSeat): string | null {
  const days = [seat.ends_on, ...ancestors(units, seat.unit).map((unit) => (unit.kind === "cohort" ? unit.ends_on : null))];
  const set = days.filter((day): day is string => Boolean(day)).sort();
  return set[0] ?? null;
}

export function isEnded(units: readonly OrgUnit[], seat: OrgSeat, today: string): boolean {
  if (seat.ended_at) return true;
  const day = endDay(units, seat);
  return day !== null && day < today;
}

/**
 * What a seat says beyond the usual: it ended, or it ends within ENDS_SOON_DAYS (0 is today, its last day). Null for a
 * seat that goes on as it should, which carries no indicator.
 */
export type SeatNote = { kind: "ended" } | { kind: "endsSoon"; days: number; day: string } | null;

export function seatNote(units: readonly OrgUnit[], seat: OrgSeat, today: string): SeatNote {
  if (isEnded(units, seat, today)) return { kind: "ended" };
  const day = endDay(units, seat);
  if (day === null) return null;
  const days = daysBetween(today, day);
  return days <= ENDS_SOON_DAYS ? { kind: "endsSoon", days, day } : null;
}

export type UnitNode = { unit: OrgUnit; seats: OrgSeat[]; children: UnitNode[] };

/**
 * The tree from the root: each unit with its seats (people first by login, then agents) and the units below it by
 * name. Archived units and ended seats are left out unless `all`.
 */
export function buildTree(chart: Pick<OrgChart, "units" | "seats">, today: string, all: boolean): UnitNode | null {
  const root = rootOf(chart.units);
  if (!root) return null;
  const shown = chart.units.filter((unit) => all || !unit.archived_at);
  const seats = chart.seats.filter((seat) => all || !isEnded(chart.units, seat, today));
  const node = (unit: OrgUnit, seen: Set<string>): UnitNode => {
    seen.add(unit.slug);
    const children = shown
      .filter((child) => child.parent === unit.slug && !seen.has(child.slug))
      .sort((a, b) => a.name.localeCompare(b.name) || a.slug.localeCompare(b.slug));
    return {
      unit,
      seats: seats.filter((seat) => seat.unit === unit.slug).sort(seatOrder),
      children: children.map((child) => node(child, seen)),
    };
  };
  return node(root, new Set());
}

function seatOrder(a: OrgSeat, b: OrgSeat): number {
  const key = (seat: OrgSeat) => `${seat.login === null ? 1 : 0}${seat.role === "lead" ? 0 : 1}${seat.login ?? seat.agent ?? ""}`;
  return key(a).localeCompare(key(b)) || a.id - b.id;
}

/** The slugs of every unit at or below `slug`, for the parent choices a unit may not take (itself and below). */
export function subtree(units: readonly OrgUnit[], slug: string): Set<string> {
  return new Set(units.filter((unit) => unit.slug === slug || isBelow(units, unit.slug, slug)).map((unit) => unit.slug));
}

// Rights, as `evo_agents.hub.org` gives them.

export type Rights = {
  /** Create a team or cohort below `parent`. */
  createUnder: (parent: OrgUnit) => boolean;
  /** Edit, archive or set the ceiling of `unit`. */
  editUnit: (unit: OrgUnit) => boolean;
  /** Add, edit or end a seat of `role` in `unit`. */
  seat: (unit: string, role: SeatRole) => boolean;
  /** Set the ceiling of `seat`: never one's own. */
  seatCeiling: (seat: OrgSeat) => boolean;
  /** The roles of a seat the viewer may add to `unit`. */
  roles: (unit: string) => SeatRole[];
  /** The levels the viewer may set: up to their own effective level, every level for a hub admin. */
  levels: Level[];
  /** Whether the viewer may write anything at all. */
  any: boolean;
};

export function rightsOf(viewer: OrgViewer, units: readonly OrgUnit[]): Rights {
  const led = viewer.leads;
  const below = (slug: string) => led.some((top) => isBelow(units, slug, top));
  const seat = (unit: string, role: SeatRole) =>
    viewer.admin || below(unit) || (led.includes(unit) && (role === "member" || role === "intern"));
  return {
    createUnder: (parent) => viewer.admin || led.includes(parent.slug) || below(parent.slug),
    editUnit: (unit) => viewer.admin || below(unit.slug),
    seat,
    seatCeiling: (target) => viewer.admin || (target.login !== viewer.login && seat(target.unit, target.role)),
    roles: (unit) => ROLES.filter((role) => seat(unit, role)),
    levels: viewer.admin ? [...LEVELS] : LEVELS.filter((level) => levelRank(level) <= levelRank(viewer.effective)),
    any: viewer.admin || led.length > 0,
  };
}

// Runs and people

export type PersonRuns = { total: number; projects: { project: string; running: number }[] };

/** Each login's runs at work, by project, the most first. */
export function runsByLogin(runs: OrgRuns | undefined): Map<string, PersonRuns> {
  const found = new Map<string, PersonRuns>();
  for (const { login, project, running } of runs?.running ?? []) {
    const entry = found.get(login) ?? { total: 0, projects: [] };
    entry.total += running;
    entry.projects.push({ project, running });
    found.set(login, entry);
  }
  for (const entry of found.values()) entry.projects.sort((a, b) => b.running - a.running || a.project.localeCompare(b.project));
  return found;
}

/** The runs list of `project` at its active runs, searched for `login`. */
export function runsHref(project: string, login: string): Route {
  return `/p/${encodeURIComponent(project)}/runs?state=active&q=${encodeURIComponent(login)}` as Route;
}

/** The people with a grant on some project who hold no live seat outside the root, by login. */
export function unplaced(users: readonly OrgUser[], chart: Pick<OrgChart, "units" | "seats">, today: string): OrgUser[] {
  const root = rootOf(chart.units)?.slug;
  const placed = new Set(
    chart.seats.filter((seat) => seat.login && seat.unit !== root && !isEnded(chart.units, seat, today)).map((seat) => seat.login),
  );
  return users.filter((user) => user.grants.length > 0 && !placed.has(user.login)).sort((a, b) => a.login.localeCompare(b.login));
}

/** The logins the chart and the hub's people know, for the pickers of a seat's person, reports_to and sponsor. */
export function knownLogins(chart: Pick<OrgChart, "seats">, users: readonly OrgUser[] = []): string[] {
  const logins = [...chart.seats.map((seat) => seat.login), ...users.map((user) => user.login)];
  return [...new Set(logins.filter((login): login is string => Boolean(login)))].sort((a, b) => a.localeCompare(b));
}
