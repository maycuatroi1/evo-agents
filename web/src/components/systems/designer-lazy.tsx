"use client";

import { RotateCw, TriangleAlert } from "lucide-react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";

import { StatePanel } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

import { DESIGNER_FRAME, type DesignerProps } from "./designer-frame";

function DesignerSkeleton() {
  return <Skeleton className={cn(DESIGNER_FRAME, "rounded-md")} aria-busy="true" data-testid="system-designer-loading" />;
}

/** The chunk of the designer failed to load (offline, a deploy replaced it): say so and offer to load the page again. */
function DesignerLoadError() {
  const t = useTranslations("systems.designer");
  return (
    <StatePanel icon={TriangleAlert} tone="danger" title={t("loadFailed")} description={t("loadFailedHint")} className={cn(DESIGNER_FRAME, "justify-center shadow-none")} testId="system-designer-error">
      <Button type="button" variant="outline" onClick={() => window.location.reload()}>
        <RotateCw aria-hidden="true" />
        {t("reload")}
      </Button>
    </StatePanel>
  );
}

/**
 * The designer, loaded on demand on the client (`next/dynamic`), so React Flow and dagre stay out of the first load
 * of the systems pages.
 */
export const SystemDesigner = dynamic<DesignerProps>(
  () => import("./designer").then((module) => module.default).catch(() => DesignerLoadError),
  { ssr: false, loading: DesignerSkeleton },
);
