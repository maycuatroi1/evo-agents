"use client";

import { Archive, CircleCheck, CircleDashed, CircleX, FlaskConical } from "lucide-react";
import { useTranslations } from "next-intl";

import type { components } from "@/lib/api/schema";

import { OtherStatusBadge, type StatusLook, StatusPill, type StatusPillProps } from "./status-badge";

export type ClaimStatus = components["schemas"]["CanvasClaim"]["status"];

/**
 * A claim's status, as the canvas pages and a problem's page show it, rather than in `STATUS_LOOKS` (web/DESIGN.md,
 * components/canvas): an assumption is neutral, testing is being checked (violet), a validated claim is green, an
 * invalidated one red, a retired one an outline. Amber is kept for what needs a person: a baseline that waits for
 * review, a test card past its deadline.
 */
export const CLAIM_STATUS_LOOKS = {
  assumption: { tone: "neutral", icon: CircleDashed },
  testing: { tone: "review", icon: FlaskConical },
  validated: { tone: "success", icon: CircleCheck },
  invalidated: { tone: "danger", icon: CircleX },
  retired: { tone: "outline", icon: Archive },
} as const satisfies Record<ClaimStatus, StatusLook>;

export function isClaimStatus(value: string): value is ClaimStatus {
  return Object.hasOwn(CLAIM_STATUS_LOOKS, value);
}

export function ClaimStatusBadge({ status, ...props }: StatusPillProps & { status: ClaimStatus }) {
  const t = useTranslations("canvas.status");
  return <StatusPill look={CLAIM_STATUS_LOOKS[status]} label={t(status)} data-status={status} data-testid="claim-status" {...props} />;
}

/** A claim's status as the hub wrote it: its pill, or the text as written for a status the web does not know yet. */
export function AnyClaimStatusBadge({ status, ...props }: StatusPillProps & { status: string }) {
  return isClaimStatus(status) ? <ClaimStatusBadge status={status} {...props} /> : <OtherStatusBadge raw={status} data-testid="claim-status" {...props} />;
}
