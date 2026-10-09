import type { Route } from "next";

import type { OverviewRun } from "@/components/home/model";
import type { LiveSignal } from "@/components/live/live-model";
import { parseRunId } from "@/components/runs/queries";
import { PROJECT_NAME } from "@/lib/queries";

/**
 * How the Monitor (`/monitor`, web/DESIGN.md) reads and writes what it watches: the tiles named in the URL, the grid's
 * shape for a number of tiles, the tiles that follow every run in flight, and the worst of the tiles' connections for
 * the top bar. Pure functions, shared by the page and its tests.
 *
 * The URL: `/monitor` alone follows every run in flight, a run that starts joining the grid; `/monitor?runs=` names the
 * tiles, each `project:id` (a project's name is `[a-z0-9-]`, so the colon is never part of it), comma separated, in
 * grid order, ended runs included: `/monitor?runs=evo-agents:12,lms:4`. An empty list is a grid with no tile.
 */

export const MONITOR_PATH = "/monitor" as Route;
export const RUNS_PARAM = "runs";
/** The most tiles the grid holds: each one reads its run every 5 seconds and keeps a stream open while it runs. */
export const MAX_TILES = 24;
/** A tile reads the trace from this many events before the run's last_seq, never the whole history. */
export const TAIL_EVENTS = 200;
/** The events a tile keeps as the run goes on: the oldest leave as new ones come. */
export const KEEP_EVENTS = 400;
/** The lines of the trace a tile shows, the latest last. */
export const TILE_LINES = 60;

export type TileRef = { project: string; id: number };

export function tileKey(tile: TileRef): string {
  return `${tile.project}:${tile.id}`;
}

/**
 * The tiles a `runs` parameter names, each once, in its order; null without the parameter (follow every run in
 * flight). An entry that is not `project:id` with a project name and a run id the API accepts is left out.
 */
export function parseTiles(param: string | null): TileRef[] | null {
  if (param === null) return null;
  const tiles: TileRef[] = [];
  const seen = new Set<string>();
  for (const part of param.split(",")) {
    const text = part.trim();
    const colon = text.lastIndexOf(":");
    if (colon <= 0) continue;
    const project = text.slice(0, colon);
    const id = parseRunId(text.slice(colon + 1));
    if (id === null || !PROJECT_NAME.test(project)) continue;
    const tile = { project, id };
    if (seen.has(tileKey(tile))) continue;
    seen.add(tileKey(tile));
    tiles.push(tile);
  }
  return tiles;
}

/** The Monitor watching `tiles`, or following every run in flight when null. */
export function monitorHref(tiles: readonly TileRef[] | null): Route {
  if (tiles === null) return MONITOR_PATH;
  return `${MONITOR_PATH}?${RUNS_PARAM}=${tiles.map(tileKey).join(",")}` as Route;
}

export function hasTile(tiles: readonly TileRef[], tile: TileRef): boolean {
  return tiles.some((other) => other.project === tile.project && other.id === tile.id);
}

/** `tiles` with `tile` added at the end, or taken out when it is there. */
export function toggleTile(tiles: readonly TileRef[], tile: TileRef): TileRef[] {
  return hasTile(tiles, tile) ? tiles.filter((other) => tileKey(other) !== tileKey(tile)) : [...tiles, { project: tile.project, id: tile.id }];
}

/**
 * While the Monitor follows every run in flight: the tiles it had, in the order they came, then each run in flight it
 * did not have yet, in the overview's order. A run that ended keeps its tile until the person closes it, or until the
 * grid would hold more than MAX_TILES: then the tiles of ended runs leave, the oldest first, so a run that starts
 * always finds a place. The same array when nothing is new, so a render that changes nothing sets no state.
 */
export function followTiles(previous: readonly TileRef[], active: readonly Pick<OverviewRun, "project" | "id">[]): readonly TileRef[] {
  const fresh = active.filter((run) => !hasTile(previous, run)).map((run) => ({ project: run.project, id: run.id }));
  if (fresh.length === 0) return previous;
  const next = [...previous, ...fresh];
  const over = next.length - MAX_TILES;
  if (over <= 0) return next;
  const leaving = new Set(next.filter((tile) => !hasTile(active, tile)).slice(0, over).map(tileKey));
  return next.filter((tile) => !leaving.has(tileKey(tile)));
}

/**
 * The grid from the xl breakpoint (1280 px): one tile takes it all, two side by side, three or four two by two, five or
 * six three by two, seven to nine three by three, all within the window; from ten, four columns of tiles of a fixed
 * height that scroll. From 768 to 1279 px the grid has at most two columns, under 768 px one.
 */
export type GridShape = { cols: number; rows: number; scroll: boolean };

export function gridShape(count: number): GridShape {
  if (count <= 1) return { cols: 1, rows: 1, scroll: false };
  if (count === 2) return { cols: 2, rows: 1, scroll: false };
  if (count <= 4) return { cols: 2, rows: 2, scroll: false };
  if (count <= 6) return { cols: 3, rows: 2, scroll: false };
  if (count <= 9) return { cols: 3, rows: 3, scroll: false };
  return { cols: 4, rows: Math.ceil(count / 4), scroll: true };
}

/** Columns between 768 and 1279 px: one tile alone, two otherwise. */
export function mediumCols(count: number): number {
  return count <= 1 ? 1 : 2;
}

/** How bad a signal is: offline, then failing, then updates by a fallback, then paused, then live. */
function rank(signal: LiveSignal): number {
  if (signal.offline) return 4;
  if (signal.failing) return 3;
  if (signal.degraded) return 2;
  if (signal.paused) return 1;
  return 0;
}

/**
 * The worst of the tiles' connections (and the list's), as the top bar's LiveIndicator shows one: the signal that is
 * worst; among equally good ones the freshest, among equally bad ones the one whose last update is oldest. Null when
 * no tile has a connection to speak of.
 */
export function worstSignal(signals: readonly LiveSignal[]): LiveSignal | null {
  let worst: LiveSignal | null = null;
  for (const signal of signals) {
    if (worst === null) {
      worst = signal;
      continue;
    }
    const a = rank(signal);
    const b = rank(worst);
    if (a > b) worst = signal;
    else if (a === b && (a === 0 ? signal.updatedAt > worst.updatedAt : signal.updatedAt < worst.updatedAt)) worst = signal;
  }
  return worst;
}

/** Where a tile's trace starts: TAIL_EVENTS events before the run's last_seq, or its first event. */
export function tailStart(lastSeq: number): number {
  return Math.max(0, lastSeq - TAIL_EVENTS);
}
