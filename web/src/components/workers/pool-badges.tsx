"use client";

import { ClockAlert, ShieldOff } from "lucide-react";
import { useTranslations } from "next-intl";

import { LookIcon, type StatusLook, StatusPill } from "@/components/status/status-badge";

/**
 * The two exceptions of the pools pages, as the kit's pill: a node the doctor holds back (it takes no run) and a join
 * token about to end. Nothing shows while a node is ready or a token has time left (web/DESIGN.md, States: a status is
 * shown only when something is wrong).
 */
export const NOT_READY_LOOK = { tone: "attention", icon: ShieldOff } as const satisfies StatusLook;
export const EXPIRING_LOOK = { tone: "attention", icon: ClockAlert } as const satisfies StatusLook;

/** "Not ready", with the hub's reason as the tooltip; nothing for a ready worker. */
export function NotReadyBadge({
  ready,
  reason,
  compact = false,
}: {
  ready: boolean;
  reason: string | null | undefined;
  compact?: boolean;
}) {
  const t = useTranslations("workers.pools");
  if (ready) return null;
  const label = t("notReady");
  const title = reason ? t("notReadyTip", { reason }) : label;
  if (compact) return <LookIcon look={NOT_READY_LOOK} label={title} data-testid="worker-not-ready" />;
  return <StatusPill look={NOT_READY_LOOK} label={label} title={title} data-testid="worker-not-ready" />;
}
