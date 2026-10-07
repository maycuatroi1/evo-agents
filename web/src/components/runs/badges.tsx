"use client";

import { Workflow } from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";

/** "Plan run": what tells a run of a whole plan from a run of one step, in tables and on the run page. A kind, so a tag. */
export function PlanRunKindBadge({ className }: { className?: string }) {
  const t = useTranslations("runs.planRun");
  return (
    <Tag className={className} data-testid="run-kind" data-kind="plan">
      <Workflow aria-hidden="true" />
      {t("kind")}
    </Tag>
  );
}
