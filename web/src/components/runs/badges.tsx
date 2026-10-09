"use client";

import { FilePen, Workflow } from "lucide-react";
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

/** "Author run": what tells a run that writes a plan from the other runs, in tables and on the run page. A kind, so a tag. */
export function AuthorRunKindBadge({ className }: { className?: string }) {
  const t = useTranslations("runs.author");
  return (
    <Tag className={className} data-testid="run-kind" data-kind="author">
      <FilePen aria-hidden="true" />
      {t("kind")}
    </Tag>
  );
}
