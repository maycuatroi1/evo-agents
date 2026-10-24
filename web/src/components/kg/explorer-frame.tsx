"use client";

import { ChevronRight, Maximize2, Minimize2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useEffect, useRef, useState } from "react";

import { Segmented } from "@/components/data/segmented";
import { Button } from "@/components/ui/button";
import type { Scope } from "@/lib/kg/explorer";
import { cn } from "@/lib/utils";

import { KindShape } from "./badges";
import { type ExplorerLayout, SCOPE_LAYOUTS } from "./explorer-layout";

/** The id of the line that tells a keyboard or screen reader user how the canvas works. */
export const HELP_ID = "kg-explorer-help";

export type Crumb = { label: string; onClick?: () => void };

/** Where the canvas stands: the overview, then the cluster or the node; each step before the last goes back to it. */
export function Trail({ crumbs }: { crumbs: Crumb[] }) {
  const t = useTranslations("kg.explorer");
  return (
    <nav aria-label={t("trail")} className="min-w-0" data-testid="kg-trail">
      <ol className="flex min-w-0 items-center gap-1 text-[13px]">
        {crumbs.map((crumb, index) => {
          const last = index === crumbs.length - 1;
          return (
            <li key={`${index}:${crumb.label}`} className={cn("flex items-center gap-1", last ? "min-w-0" : "shrink-0")}>
              {index > 0 ? <ChevronRight className="size-3.5 shrink-0 text-fg-subtle" aria-hidden="true" /> : null}
              {last || !crumb.onClick ? (
                <span aria-current={last ? "location" : undefined} className="truncate font-medium text-foreground" title={crumb.label}>
                  {crumb.label}
                </span>
              ) : (
                <button
                  type="button"
                  onClick={crumb.onClick}
                  className="max-w-40 cursor-pointer truncate rounded-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                  title={crumb.label}
                >
                  {crumb.label}
                </button>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

/** What the canvas draws, in words: sizes and lines for the overview, kinds and marks for nodes. */
export function Legend({ scope, kinds, counts }: { scope: Scope["kind"]; kinds: string[]; counts: string }) {
  const t = useTranslations("kg.explorer.legend");
  return (
    <details className="group pointer-events-auto max-w-[min(22rem,calc(100%-4.5rem))] rounded-md border bg-card/95 text-xs shadow-raised" data-testid="kg-legend">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-2.5 py-1.5 text-muted-foreground select-none max-md:min-h-11 [&::-webkit-details-marker]:hidden">
        <ChevronRight className="size-3.5 shrink-0 transition-transform group-open:rotate-90" aria-hidden="true" />
        <span className="font-medium text-foreground">{t("title")}</span>
        <span className="whitespace-nowrap tabular-nums" data-testid="kg-explorer-counts">
          {counts}
        </span>
      </summary>
      <div className="flex flex-col gap-2 border-t px-2.5 py-2 text-muted-foreground">
        {scope === "overview" ? (
          <ul className="flex flex-col gap-1">
            <li className="flex items-center gap-2">
              <span className="size-3 shrink-0 rounded-full bg-chart-1" aria-hidden="true" />
              {t("clusterSize")}
            </li>
            <li className="flex items-center gap-2">
              <span className="h-1 w-3 shrink-0 rounded-full bg-fg-subtle" aria-hidden="true" />
              {t("linkWidth")}
            </li>
            <li className="flex items-center gap-2">
              <span className="w-3 shrink-0 border-t border-dashed border-fg-subtle" aria-hidden="true" />
              {t("weakLink")}
            </li>
          </ul>
        ) : (
          <ul className="flex flex-wrap gap-x-3 gap-y-1">
            {kinds.map((kind) => (
              <li key={kind} className="inline-flex items-center gap-1.5">
                <KindShape kind={kind} />
                {kind}
              </li>
            ))}
            {scope === "cluster" ? (
              <li className="inline-flex items-center gap-1.5">
                <span className="h-2.5 w-3.5 rounded-xs border bg-muted" aria-hidden="true" />
                {t("part")}
              </li>
            ) : (
              <>
                <li className="inline-flex items-center gap-1.5">
                  <span className="size-3 rounded-full border-[3px] border-brand" aria-hidden="true" />
                  {t("seed")}
                </li>
                <li className="inline-flex items-center gap-1.5">
                  <span className="size-3 rounded-full border-[2.5px] border-foreground" aria-hidden="true" />
                  {t("expanded")}
                </li>
                <li className="inline-flex items-center gap-1.5">
                  <span className="h-2.5 w-4 rounded-xs border-[1.5px] border-dashed border-fg-subtle" aria-hidden="true" />
                  {t("more")}
                </li>
              </>
            )}
          </ul>
        )}
        <p>{t("help")}</p>
      </div>
    </details>
  );
}

/**
 * The explorer's frame: the rail on the left (search on top, the rest scrolling), the canvas with its toolbar in the
 * middle and the inspector on the right while something is selected. From 1024 px it fills the height left under the
 * page head and nothing but its columns scrolls; below, the parts stack: search, canvas, inspector, then the rail.
 * Full screen puts the whole frame on the screen.
 */
export function Workspace({
  search,
  rail,
  trail,
  layout,
  onLayout,
  scope,
  canvas,
  legend,
  inspector,
  announcement,
  status,
}: {
  search: ReactNode;
  rail: ReactNode;
  trail: ReactNode;
  layout: ExplorerLayout;
  onLayout: (layout: ExplorerLayout) => void;
  scope: Scope["kind"];
  canvas: ReactNode;
  legend: ReactNode;
  inspector: ReactNode | null;
  announcement: string;
  /** A short line beside the trail while something loads ("Reading neighbours"). */
  status?: string | null;
}) {
  const t = useTranslations("kg.explorer");
  const frame = useRef<HTMLDivElement>(null);
  const [full, setFull] = useState(false);
  useEffect(() => {
    const change = () => setFull(document.fullscreenElement === frame.current);
    document.addEventListener("fullscreenchange", change);
    return () => document.removeEventListener("fullscreenchange", change);
  }, []);
  const toggleFull = () => {
    if (document.fullscreenElement) void document.exitFullscreen();
    else void frame.current?.requestFullscreen?.();
  };
  const layouts = SCOPE_LAYOUTS[scope];
  return (
    <div
      ref={frame}
      className={cn(
        "grid min-h-0 overflow-hidden rounded-md border bg-background",
        "[grid-template-areas:'search'_'canvas'_'inspector'_'rail']",
        "lg:flex-1 lg:grid-rows-[auto_minmax(0,1fr)]",
        inspector
          ? "lg:grid-cols-[17.5rem_minmax(0,1fr)_21rem] lg:[grid-template-areas:'search_canvas_inspector'_'rail_canvas_inspector']"
          : "lg:grid-cols-[17.5rem_minmax(0,1fr)] lg:[grid-template-areas:'search_canvas'_'rail_canvas']",
        full && "h-screen rounded-none border-0",
      )}
      data-testid="kg-explorer"
      data-scope={scope}
    >
      <div className="max-h-[50vh] min-w-0 overflow-y-auto [grid-area:search] lg:max-h-[45vh] lg:border-r">{search}</div>
      <div className="min-h-0 min-w-0 overflow-y-auto border-t [grid-area:rail] lg:border-t-0 lg:border-r" data-testid="kg-rail">
        {rail}
      </div>
      <div className="flex min-h-0 min-w-0 flex-col border-b [grid-area:canvas] lg:border-b-0">
        <div className="flex h-11 shrink-0 items-center gap-3 border-b px-3">
          <div className="min-w-0 flex-1">{trail}</div>
          {status ? (
            <span className="hidden shrink-0 text-xs text-muted-foreground sm:inline" role="status">
              {status}
            </span>
          ) : null}
          {layouts.length > 1 ? (
            <Segmented
              label={t("layout")}
              value={layout}
              onChange={onLayout}
              options={layouts.map((value) => ({ value, label: t(`layouts.${value}`), testId: `kg-layout-${value}` }))}
            />
          ) : null}
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            className="shrink-0 cursor-pointer max-md:hidden"
            aria-label={full ? t("exitFullscreen") : t("fullscreen")}
            title={full ? t("exitFullscreen") : t("fullscreen")}
            aria-pressed={full}
            onClick={toggleFull}
          >
            {full ? <Minimize2 aria-hidden="true" /> : <Maximize2 aria-hidden="true" />}
          </Button>
        </div>
        <div className="relative h-[62vh] min-h-[22rem] bg-[radial-gradient(var(--border)_1px,transparent_1px)] [background-size:18px_18px] lg:h-auto lg:min-h-0 lg:flex-1">
          {canvas}
          <div className="pointer-events-none absolute bottom-3 left-3 flex">{legend}</div>
        </div>
        <p id={HELP_ID} className="sr-only">
          {t("legend.help")}
        </p>
      </div>
      {inspector ? (
        <div className="flex min-h-0 min-w-0 flex-col border-b [grid-area:inspector] lg:border-b-0 lg:border-l">{inspector}</div>
      ) : null}
      <p className="sr-only" aria-live="polite" data-testid="kg-explorer-announcement">
        {announcement}
      </p>
    </div>
  );
}
