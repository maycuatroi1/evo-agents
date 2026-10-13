"use client";

import { useReactFlow } from "@xyflow/react";
import { Maximize, Minus, Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { TONE_TEXT } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import type { PlanGraph } from "@/lib/plan-graph/layout";
import { FIT } from "@/lib/plan-graph/viewport";
import { STEP_STATUSES } from "@/lib/plans";
import { cn } from "@/lib/utils";

import { stepLook, useStepStatusText } from "./status";
import { LineSample } from "./step-graph-parts";

/**
 * The Graph view's bar above the canvas: the legend and the zoom buttons, kept out of the canvas so no step sits under
 * them. Fit shows the whole plan, never past zoom 1 (FIT of lib/plan-graph/viewport.ts).
 */

const reducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Moves of the canvas take this long, or no time at all under prefers-reduced-motion. */
export const motion = (duration: number) => ({ duration: reducedMotion() ? 0 : duration });

export function Legend({ graph, children }: { graph: PlanGraph; children?: ReactNode }) {
  const t = useTranslations("plans.graph");
  const status = useStepStatusText();
  const notDrawn = graph.edges.filter((edge) => edge.kind !== "drawn").length;
  return (
    <div
      className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-b px-3 py-2 text-xs text-muted-foreground"
      data-testid="graph-legend"
    >
      {STEP_STATUSES.map((group) => {
        const look = stepLook(group);
        const count = graph.nodes.filter((node) => node.step.group === group).length;
        return (
          <span key={group} className="inline-flex items-center gap-1.5" data-testid={`graph-count-${group}`}>
            <look.icon className={cn("size-3.5", TONE_TEXT[look.tone])} aria-hidden="true" />
            <span className="font-medium text-foreground tabular-nums">{count}</span>
            {status(group)}
          </span>
        );
      })}
      <span className="inline-flex items-center gap-1.5" data-testid="graph-count-ready">
        <span className="font-medium text-foreground tabular-nums">{graph.nodes.filter((node) => node.ready).length}</span>
        {t("ready")}
      </span>
      <span className="inline-flex items-center gap-1.5">
        <LineSample dashed={false} />
        {t("lineDone")}
      </span>
      <span className="inline-flex items-center gap-1.5">
        <LineSample dashed />
        {t("lineNotDone")}
      </span>
      {notDrawn ? (
        <span title={t("notDrawnHint")} data-testid="graph-not-drawn">
          {t("notDrawn", { count: notDrawn })}
        </span>
      ) : null}
      {children}
    </div>
  );
}

export function Zoom() {
  const t = useTranslations("plans.graph");
  const flow = useReactFlow();
  const buttons = [
    { label: t("zoomIn"), icon: Plus, run: () => flow.zoomIn(motion(180)) },
    { label: t("zoomOut"), icon: Minus, run: () => flow.zoomOut(motion(180)) },
    { label: t("fit"), icon: Maximize, run: () => flow.fitView({ ...FIT, ...motion(180) }) },
  ];
  return (
    <div className="ml-auto flex gap-1">
      {buttons.map(({ label, icon: Icon, run }) => (
        <Button
          key={label}
          type="button"
          variant="ghost"
          size="icon-sm"
          className="cursor-pointer max-md:size-11"
          aria-label={label}
          title={label}
          onClick={() => void run()}
        >
          <Icon aria-hidden="true" />
        </Button>
      ))}
    </div>
  );
}
