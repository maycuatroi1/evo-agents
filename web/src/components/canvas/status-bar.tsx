"use client";

import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";

import type { ClaimStatus } from "./api";
import { STATUSES } from "./model";

/** The solid mark of each status's tone (web/DESIGN.md, Colour: states), retired as the strong rule an outline has. */
const MARK: Record<ClaimStatus, string> = {
  assumption: "bg-neutral-solid",
  testing: "bg-review-solid",
  validated: "bg-success-solid",
  invalidated: "bg-danger-solid",
  retired: "bg-border-strong",
};

/**
 * A canvas's claims by status as one bar of segments, each as wide as its share, 2 px of the card between them. It is
 * an image with its counts in words, so colour is never the only cue; an empty canvas draws an empty track.
 */
export function StatusBar({ counts, className }: { counts: Record<ClaimStatus, number>; className?: string }) {
  const t = useTranslations("canvas.status");
  const total = STATUSES.reduce((sum, status) => sum + counts[status], 0);
  const words = total === 0 ? t("none") : STATUSES.filter((status) => counts[status] > 0).map((status) => `${counts[status]} ${t(status).toLowerCase()}`).join(", ");
  return (
    <span role="img" aria-label={words} title={words} className={cn("flex h-2 min-w-16 gap-0.5 overflow-hidden rounded-full bg-muted", className)} data-testid="status-bar">
      {total === 0
        ? null
        : STATUSES.filter((status) => counts[status] > 0).map((status) => (
            <span key={status} className={cn("h-full first:rounded-l-full last:rounded-r-full", MARK[status])} style={{ flexGrow: counts[status] }} data-status={status} />
          ))}
    </span>
  );
}
