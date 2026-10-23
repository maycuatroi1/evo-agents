"use client";

import { CircleCheck, CircleDot, CircleMinus } from "lucide-react";
import { useTranslations } from "next-intl";

import { type StatusLook, StatusPill, type StatusPillProps } from "@/components/status/status-badge";
import type { ProblemStatus } from "@/lib/problem-links";

/**
 * A problem's status as a pill: open is neutral (watched, not a state that needs anyone), solved green, dropped an
 * outline. Its own file, so the Loop's pages that show no problem do not load the Problems pages' model.
 */
export const PROBLEM_LOOKS: Record<ProblemStatus, StatusLook> = {
  open: { tone: "neutral", icon: CircleDot },
  solved: { tone: "success", icon: CircleCheck },
  dropped: { tone: "outline", icon: CircleMinus },
};

export function ProblemStatusBadge({ status, ...props }: StatusPillProps & { status: ProblemStatus }) {
  const t = useTranslations("problems.status");
  return <StatusPill look={PROBLEM_LOOKS[status]} label={t(status)} data-status={status} data-testid="problem-status" {...props} />;
}
