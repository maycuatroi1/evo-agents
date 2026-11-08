"use client";

import { Eye, EyeOff } from "lucide-react";
import { useTranslations } from "next-intl";

import { EDGE_KINDS, type EdgeKind } from "@/lib/systems";
import { cn } from "@/lib/utils";

import { EDGE_LOOK } from "./designer-edge";

/** A short line in the colour and dash of `kind`. */
function Swatch({ kind }: { kind: EdgeKind }) {
  const look = EDGE_LOOK[kind];
  return (
    <svg width="22" height="6" viewBox="0 0 22 6" aria-hidden="true" className="shrink-0">
      <line x1="1" y1="3" x2="21" y2="3" stroke={look.colour} strokeWidth="2" strokeDasharray={look.dash} strokeLinecap="round" />
    </svg>
  );
}

/**
 * The key to the canvas's lines, at its bottom left: each kind of edge the system has, its colour and dash and its
 * word. The Curator's lines are not drawn unless asked (its mark on the nodes it optimizes says as much); their entry
 * is the toggle that draws them. On a phone only that toggle stays, so the key does not cover the system; each line
 * still names its kind in its label.
 */
export function SystemLegend({ kinds, showOptimizes, onShowOptimizes }: { kinds: ReadonlySet<EdgeKind>; showOptimizes: boolean; onShowOptimizes: (show: boolean) => void }) {
  const t = useTranslations("systems.designer");
  const present = EDGE_KINDS.filter((kind) => kinds.has(kind));
  if (!present.length) return null;
  // On a phone only the toggle shows: with no Curator lines there is nothing to show there.
  const onlyKinds = !kinds.has("optimizes");
  return (
    <div
      role="group"
      aria-label={t("edges")}
      className={cn("flex max-w-[calc(100vw-3rem)] flex-wrap items-center gap-x-3 gap-y-1 rounded-md border bg-card/95 px-2.5 py-1.5 text-xs text-muted-foreground", onlyKinds && "max-sm:hidden")}
      data-testid="system-legend"
    >
      {present.map((kind) =>
        kind === "optimizes" ? (
          <button
            key={kind}
            type="button"
            aria-pressed={showOptimizes}
            title={t("showOptimizes")}
            onClick={() => onShowOptimizes(!showOptimizes)}
            className={cn(
              "-mx-1 inline-flex min-h-6 cursor-pointer items-center gap-1.5 rounded-sm px-1 transition-colors duration-fast hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
              showOptimizes && "text-foreground",
            )}
            data-testid="system-legend-optimizes"
          >
            <Swatch kind={kind} />
            {t(`edgeKinds.${kind}`)}
            {showOptimizes ? <Eye className="size-3.5" aria-hidden="true" /> : <EyeOff className="size-3.5" aria-hidden="true" />}
          </button>
        ) : (
          <span key={kind} className="inline-flex min-h-6 items-center gap-1.5 max-sm:hidden">
            <Swatch kind={kind} />
            {t(`edgeKinds.${kind}`)}
          </span>
        ),
      )}
    </div>
  );
}
