"use client";

import { type QueryState, useQueries, useQuery } from "@tanstack/react-query";
import { Info, MonitorPlay } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback, useId, useMemo, useState, useSyncExternalStore } from "react";

import { HOME_IDLE_MS, HOME_LIVE_MS, hasWork, homeRefreshInterval, type Overview, type OverviewDecision, type OverviewRun } from "@/components/home/model";
import { useLiveSignal } from "@/components/live/live-context";
import { querySignal } from "@/components/live/live-model";
import { runQuery } from "@/components/runs/queries";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useMediaQuery, XL_QUERY } from "@/hooks/use-media-query";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { overviewQuery, whoamiQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { FlightList } from "./flight-list";
import {
  followTiles,
  gridShape,
  MAX_TILES,
  mediumCols,
  monitorHref,
  parseTiles,
  RUNS_PARAM,
  type TileRef,
  tileKey,
  toggleTile,
  worstSignal,
} from "./model";
import { RunTile } from "./run-tile";
import { TileSignals } from "./signals";

/**
 * The Monitor (`/monitor`, web/DESIGN.md): the runs in flight of every project of the visitor's grants, in a list, and
 * the runs the visitor watches as live tiles in a grid that shares out the window. Without `?runs=` it follows every
 * run in flight, a run that starts joining the grid; checking or unchecking a run in the list, or closing a tile,
 * writes the tiles to the URL (`?runs=project:id,...`, ended runs included), so a reload or the link shows the same
 * grid. From xl the page is one window tall: the list a column that scrolls inside itself, the grid beside it, up to
 * nine tiles within the window and from ten four columns that scroll. The top bar's LiveIndicator says the worst of the
 * tiles' connections and the list's.
 */

const XL_COLS: Record<number, string> = { 1: "xl:grid-cols-1", 2: "xl:grid-cols-2", 3: "xl:grid-cols-3", 4: "xl:grid-cols-4" };
const XL_ROWS: Record<number, string> = { 1: "xl:grid-rows-1", 2: "xl:grid-rows-2", 3: "xl:grid-rows-3" };


function MonitorSkeleton() {
  return (
    <div className="grid gap-4 xl:grid-cols-[18rem_minmax(0,1fr)]">
      <div className="overflow-hidden rounded-md border bg-card shadow-raised">
        <div className="flex h-12 items-center border-b px-4">
          <Skeleton className="h-3.5 w-24 rounded-xs" />
        </div>
        {[0, 1, 2].map((row) => (
          <div key={row} className="flex flex-col gap-1.5 border-t px-4 py-3 first:border-t-0">
            <Skeleton className="h-3 w-[40%] rounded-xs" />
            <Skeleton className="h-3 w-[70%] rounded-xs" />
          </div>
        ))}
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        {[0, 1].map((tile) => (
          <Skeleton key={tile} className="h-[20rem] w-full rounded-md" />
        ))}
      </div>
    </div>
  );
}

/** The list's own read of the overview as the top bar weighs it against the tiles: its failures, its interval. */
function useOverviewSignal() {
  const query = useQuery({ ...overviewQuery(browserApi), refetchInterval: homeRefreshInterval });
  const state = {
    dataUpdatedAt: query.dataUpdatedAt,
    errorUpdatedAt: query.errorUpdatedAt,
    fetchFailureCount: query.failureCount,
    fetchStatus: query.fetchStatus,
  } as QueryState<unknown, unknown>;
  return { signal: querySignal(state, hasWork(query.data) ? HOME_LIVE_MS : HOME_IDLE_MS), refetch: query.refetch };
}

function Note({ children, testId }: { children: string; testId: string }) {
  return (
    <p role="note" className="flex items-start gap-2 rounded-md bg-surface-sunken px-3 py-2 text-xs text-muted-foreground" data-testid={testId}>
      <Info className="mt-px size-3.5 shrink-0" aria-hidden="true" />
      {children}
    </p>
  );
}

function Monitor({ overview, viewer }: { overview: Overview; viewer: string | null }) {
  const t = useTranslations("monitor");
  const gridId = useId();
  const params = useSearchParams();
  const param = params.get(RUNS_PARAM);
  const chosen = useMemo(() => parseTiles(param), [param]);
  const following = chosen === null;

  // While following, the tiles it had stay (a run that ended keeps its tile) and each new run in flight joins them.
  const [followed, setFollowed] = useState<readonly TileRef[]>([]);
  const merged = following ? followTiles(followed, overview.active_runs) : followed;
  if (merged !== followed) setFollowed(merged);
  const all = chosen ?? merged;
  const tiles = useMemo(() => all.slice(0, MAX_TILES), [all]);

  // Each tile's run, read here so the page knows which runs the visitor cannot read (403, 404), and leaves them out.
  const runs = useQueries({ queries: tiles.map((tile) => runQuery(browserApi, tile.project, tile.id)) });
  const shown = tiles.flatMap((tile, index) => {
    const query = runs[index];
    const unreadable = query.isError && (query.error.kind === "forbidden" || query.error.kind === "not_found");
    return unreadable ? [] : [{ tile, query }];
  });
  const hidden = tiles.length - shown.length;

  const write = useCallback((next: readonly TileRef[] | null) => {
    window.history.replaceState(null, "", monitorHref(next));
  }, []);
  const toggle = (tile: TileRef) => write(toggleTile(tiles, tile));
  const close = (tile: TileRef) => write(tiles.filter((other) => tileKey(other) !== tileKey(tile)));
  const followAll = () => {
    setFollowed([]);
    write(null);
  };

  // The top bar: the worst of the tiles' streams and of the list's own reads, while any tile has a stream to speak of.
  const [store] = useState(() => new TileSignals());
  const tileSignals = useSyncExternalStore(store.subscribe, store.snapshot, store.serverSnapshot);
  const list = useOverviewSignal();
  useLiveSignal(tileSignals.length > 0 ? worstSignal([...tileSignals, list.signal]) : null, {
    retry: () => {
      void list.refetch();
      for (const query of runs) if (query.isError) void query.refetch();
    },
  });

  const flights = useMemo(() => {
    const byKey = new Map<string, OverviewRun>();
    for (const run of [...overview.recent_runs, ...overview.active_runs]) byKey.set(tileKey(run), run);
    return byKey;
  }, [overview]);
  const decisions = useMemo(() => {
    const byKey = new Map<string, OverviewDecision>();
    // The visitor's own first (the overview's order), so a run with several open decisions says "Waiting for you".
    for (const decision of [...overview.open_decisions].reverse()) byKey.set(`${decision.project}:${decision.run_id}`, decision);
    return byKey;
  }, [overview]);

  const shape = gridShape(shown.length);
  const xl = useMediaQuery(XL_QUERY);

  const header = (
    <PageHeader
      title={t("title")}
      sub={
        <span data-testid="monitor-mode" data-mode={following ? "follow" : "chosen"}>
          {following ? t("following") : t("chosen", { count: shown.length })}
        </span>
      }
      actions={
        following ? null : (
          <Button variant="secondary" onClick={followAll} data-testid="monitor-follow-all">
            <MonitorPlay aria-hidden="true" />
            {t("followAll")}
          </Button>
        )
      }
    />
  );

  if (following && tiles.length === 0) {
    return (
      <>
        {header}
        <EmptyState icon={MonitorPlay} title={t("empty.title")} description={t("empty.description")} />
      </>
    );
  }

  return (
    // From xl the page is one window tall (the window less the top bar and the gutters), the list and the grid side by
    // side below the head; at least 30rem, so a short window scrolls the page past the head instead of squeezing them.
    <div className="flex flex-col gap-6 xl:h-[calc(100dvh-6.25rem)] xl:min-h-fit" data-testid="monitor">
      {header}
      <div
        className="grid gap-4 xl:min-h-[30rem] xl:flex-1 xl:grid-cols-[18rem_minmax(0,1fr)] xl:grid-rows-[minmax(0,1fr)] xl:[contain:size] 2xl:grid-cols-[20rem_minmax(0,1fr)]"
        data-testid="monitor-split"
      >
        <FlightList runs={overview.active_runs} tiles={tiles} onToggle={toggle} />
        <section aria-labelledby={gridId} className="flex min-h-0 min-w-0 flex-col gap-3">
          <h2 id={gridId} className="sr-only">
            {t("grid.label")}
          </h2>
          {hidden > 0 ? <Note testId="monitor-hidden">{t("grid.hidden", { count: hidden })}</Note> : null}
          {!following && all.length > MAX_TILES ? <Note testId="monitor-capped">{t("grid.capped", { max: MAX_TILES })}</Note> : null}
          {shown.length === 0 ? (
            <EmptyState icon={MonitorPlay} title={t("grid.emptyTitle")} description={t("grid.emptyText")} className="xl:flex-1 xl:justify-center">
              <Button variant="secondary" onClick={followAll} data-testid="monitor-empty-follow">
                <MonitorPlay aria-hidden="true" />
                {t("followAll")}
              </Button>
            </EmptyState>
          ) : (
            <ul
              aria-labelledby={gridId}
              // From ten tiles the grid scrolls inside itself, a region Tab reaches.
              tabIndex={xl && shape.scroll ? 0 : undefined}
              className={cn(
                "grid min-w-0 auto-rows-[20rem] grid-cols-1 gap-3 md:auto-rows-[22rem]",
                mediumCols(shown.length) === 2 && "md:grid-cols-2",
                "xl:min-h-0 xl:flex-1",
                shape.scroll
                  ? "xl:-m-1 xl:auto-rows-[17rem] xl:grid-cols-4 xl:content-start xl:overflow-y-auto xl:overscroll-contain xl:p-1 xl:focus-visible:outline-offset-[-2px]"
                  : cn(XL_COLS[shape.cols], XL_ROWS[shape.rows]),
              )}
              data-testid="monitor-grid"
              data-count={shown.length}
              data-cols={shape.cols}
              data-scroll={shape.scroll}
            >
              {shown.map(({ tile, query }) => (
                <li key={tileKey(tile)} className="min-h-0 min-w-0">
                  <RunTile
                    tile={tile}
                    query={query}
                    flight={flights.get(tileKey(tile)) ?? null}
                    decision={decisions.get(tileKey(tile)) ?? null}
                    viewer={viewer}
                    signals={store}
                    onClose={() => close(tile)}
                  />
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}

export function MonitorPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("monitor");
  const { data: me } = useQuery(whoamiQuery(browserApi));
  // The page's main query: the list, asked every 5 seconds while a run is in flight; the top bar says from it (and from
  // the tiles' streams) whether the page is current.
  const state = useHubQuery({ ...overviewQuery(browserApi), refetchInterval: homeRefreshInterval }, initialError, { live: true });
  if (state.status === "success") return <Monitor overview={state.data} viewer={me?.login ?? null} />;
  return (
    <>
      <PageHeader title={t("title")} />
      <QueryView state={state} loading={<MonitorSkeleton />}>
        {() => null}
      </QueryView>
    </>
  );
}
