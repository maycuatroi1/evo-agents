"use client";

import { CircleX, ClockAlert, RefreshCwOff } from "lucide-react";
import { useTranslations } from "next-intl";

import { LookIcon, type StatusLook, StatusPill } from "@/components/status/status-badge";
import { useNow } from "@/hooks/use-now";

import type { Worker } from "./queries";
import { type UpdateProblem, updateProblems } from "./self-update";

/**
 * What went wrong with a worker's updates, as the kit's pill: red for an update that failed, amber where a person is
 * needed (a daemon that cannot update itself, one that has waited half a day for an idle moment). Only the workers pages
 * show these, so the looks live here rather than in `STATUS_LOOKS`; their words are `workers.update` in messages.
 */
export const UPDATE_LOOKS = {
  failed: { tone: "danger", icon: CircleX },
  unsupported: { tone: "attention", icon: RefreshCwOff },
  waiting: { tone: "attention", icon: ClockAlert },
} as const satisfies Record<UpdateProblem, StatusLook>;

/**
 * The most serious update problem of a worker in a row of the list, and nothing at all while its updates go as they
 * should. `compact`, for a phone's row, shows the icon alone with its words for screen readers and as a tooltip.
 */
export function UpdateBadge({ worker, compact = false }: { worker: Worker; compact?: boolean }) {
  const t = useTranslations("workers.update");
  // The browser's clock only while a wait could grow long; null during hydration, as on the server.
  const now = useNow(worker.self_update?.state === "waiting");
  const problem = updateProblems(worker, now)[0];
  if (!problem) return null;
  const label = t(`badge.${problem}`);
  if (compact) {
    return <LookIcon look={UPDATE_LOOKS[problem]} label={label} data-testid="worker-update" data-problem={problem} />;
  }
  return (
    <StatusPill
      look={UPDATE_LOOKS[problem]}
      label={label}
      title={t(`tip.${problem}`)}
      data-testid="worker-update"
      data-problem={problem}
    />
  );
}
