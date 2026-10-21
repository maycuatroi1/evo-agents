"use client";

import { Images, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { FacetGroup } from "@/components/data/facet-group";
import { useByteSize } from "@/components/data/byte-size";
import { SOURCE_ICON } from "@/components/data/output-source";
import { OutputTile } from "@/components/data/output-tile";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { OUTPUT_SOURCES, type OutputSource, type RunOutput } from "@/lib/outputs";
import { cn } from "@/lib/utils";

import type { RunOutputsState } from "./use-run-outputs";

/**
 * The Outputs tab of a run's page: the files the run's agent made, newest first, as a grid of tiles (an image as its
 * thumbnail, loaded lazily; any other file as its icon and extension) that open the page's viewer, with chips by source
 * (sent by the agent, taken out of tools' results, left in the run's output folder) and how many files and bytes the
 * run holds, said again to screen readers as files arrive. When the project keeps no images of tools' results it says
 * so, since a run's screenshots are then missing on purpose. `fill`, as the other tabs: the grid scrolls inside the
 * session from xl (at every width on a phone's screen).
 */
export function RunOutputsPanel({ state, runId, fill }: { state: RunOutputsState; runId: number; fill: boolean | "always" }) {
  const t = useTranslations("runs.detail.outputs");
  const size = useByteSize();
  const [source, setSource] = useState<OutputSource | null>(null);
  const outputs = state.data?.outputs;
  const newest = useMemo(() => (outputs ? [...outputs].reverse() : []), [outputs]);
  const counts = useMemo(() => {
    const found: Record<OutputSource, number> = { sent: 0, tool: 0, outputs: 0 };
    for (const output of newest) found[output.source] += 1;
    return found;
  }, [newest]);
  const shown = useMemo(() => (source ? newest.filter((output) => output.source === source) : newest), [newest, source]);
  const scroll = cn(
    "min-w-0 py-3",
    fill === "always" ? "min-h-0 flex-1 overflow-y-auto overscroll-contain" : fill && "xl:min-h-0 xl:flex-1 xl:overflow-y-auto xl:overscroll-contain",
  );

  if (state.status === "pending") {
    return (
      <div role="status" aria-label={t("loading")} className={cn(scroll, "grid grid-cols-[repeat(auto-fill,minmax(9.5rem,1fr))] gap-3")} data-testid="run-outputs-loading">
        {Array.from({ length: 6 }, (_, index) => (
          <Skeleton key={index} className="aspect-[4/3] w-full animate-appear-late rounded-md" />
        ))}
      </div>
    );
  }
  if (state.status === "error" || !state.data) {
    return (
      <div className="flex items-center gap-2 py-6 text-sm text-danger" role="alert" data-testid="run-outputs-error">
        <TriangleAlert className="size-4 shrink-0" aria-hidden="true" />
        {t("failed")}
        <Button type="button" variant="outline" size="sm" onClick={state.retry}>
          {t("retry")}
        </Button>
      </div>
    );
  }
  const data = state.data;
  const open = (output: RunOutput) => state.open(output, shown);
  return (
    <div className={cn("flex min-w-0 flex-col", fill === "always" ? "min-h-0 flex-1" : fill && "xl:min-h-0 xl:flex-1")} data-testid="run-outputs">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b py-3">
        <FacetGroup
          label={t("filter")}
          options={[
            { value: null, label: t("all"), count: newest.length },
            ...OUTPUT_SOURCES.filter((value) => counts[value] > 0).map((value) => ({ value, label: t(`sources.${value}`), icon: SOURCE_ICON[value], count: counts[value] })),
          ]}
          selected={source}
          onSelect={(value) => setSource(value as OutputSource | null)}
          countLabel={(count) => t("count", { count })}
          testId="run-outputs-facets"
        />
        <span className="ml-auto text-xs text-fg-subtle tabular-nums" aria-live="polite" data-testid="run-outputs-summary">
          {t("summary", { count: data.count, size: size(data.bytes) })}
        </span>
        {data.tool_images ? null : (
          <p className="basis-full text-xs text-muted-foreground" data-testid="run-outputs-tool-images-off">
            {t("toolImagesOff")}
          </p>
        )}
      </div>
      <div className={scroll}>
        {shown.length === 0 ? (
          <p className="flex items-center gap-2 py-4 text-sm text-muted-foreground" data-testid="run-outputs-empty">
            <Images className="size-4" aria-hidden="true" />
            {newest.length ? t("noMatch") : t("none")}
          </p>
        ) : (
          <ul aria-label={t("label", { id: runId })} className="grid grid-cols-[repeat(auto-fill,minmax(9.5rem,1fr))] gap-3 sm:grid-cols-[repeat(auto-fill,minmax(11rem,1fr))]" data-testid="run-outputs-grid">
            {shown.map((output) => (
              <li key={output.id} className="min-w-0">
                <OutputTile output={output} onOpen={() => open(output)} />
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
