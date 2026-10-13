"use client";

import { Columns3, TriangleAlert } from "lucide-react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";

import { StatePanel } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

import { GRAPH_FRAME, type StepGraphProps } from "./step-graph-frame";

function GraphSkeleton() {
  return (
    <Skeleton className={cn(GRAPH_FRAME, "rounded-md")} aria-busy="true" data-testid="step-graph-loading" />
  );
}

/** The chunk of the graph failed to load (offline, a deploy replaced it): say so and offer Board, which needs nothing more. */
function GraphLoadError({ onBoard }: StepGraphProps) {
  const t = useTranslations("plans.graph");
  return (
    <StatePanel
      icon={TriangleAlert}
      tone="danger"
      title={t("loadFailed")}
      description={t("loadFailedHint")}
      className={cn(GRAPH_FRAME, "justify-center shadow-none")}
      testId="step-graph-error"
    >
      <Button type="button" variant="outline" onClick={onBoard}>
        <Columns3 aria-hidden="true" />
        {t("useBoard")}
      </Button>
    </StatePanel>
  );
}

/**
 * The Graph view, loaded on demand on the client (`next/dynamic`), so React Flow and dagre stay out of the plan page's
 * first load and Board and List never fetch them.
 */
export const StepGraph = dynamic<StepGraphProps>(
  () => import("./step-graph").then((module) => module.default).catch(() => GraphLoadError),
  { ssr: false, loading: GraphSkeleton },
);
