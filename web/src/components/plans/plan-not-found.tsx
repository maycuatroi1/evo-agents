"use client";

import { useTranslations } from "next-intl";

import { NotFoundState } from "@/components/states/states";

/** The API answers 404 alike for a plan that does not exist and one above the reader's level, so the page does too. */
export function PlanNotFound({ project, planId, step }: { project: string; planId: string; step?: string }) {
  const t = useTranslations("plans.notFound");
  if (step !== undefined) {
    return <NotFoundState title={t("stepTitle", { step, plan: planId })} description={t("stepDescription")} />;
  }
  return <NotFoundState title={t("title", { plan: planId })} description={t("description", { project })} />;
}
