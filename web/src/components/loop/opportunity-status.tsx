"use client";

import { CircleCheck, CircleDot, CircleMinus, CircleX } from "lucide-react";
import { useTranslations } from "next-intl";

import { type StatusLook, StatusPill, type StatusPillProps } from "@/components/status/status-badge";
import type { OpportunityStatus } from "@/lib/opportunity-links";

/** An opportunity's status as a pill: open is neutral, captured green, missed attention, dropped an outline. */
export const OPPORTUNITY_LOOKS: Record<OpportunityStatus, StatusLook> = {
  open: { tone: "neutral", icon: CircleDot },
  captured: { tone: "success", icon: CircleCheck },
  missed: { tone: "attention", icon: CircleX },
  dropped: { tone: "outline", icon: CircleMinus },
};

export function OpportunityStatusBadge({ status, ...props }: StatusPillProps & { status: OpportunityStatus }) {
  const t = useTranslations("opportunities.status");
  return <StatusPill look={OPPORTUNITY_LOOKS[status]} label={t(status)} data-status={status} data-testid="opportunity-status" {...props} />;
}
