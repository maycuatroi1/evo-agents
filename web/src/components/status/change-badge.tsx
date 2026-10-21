"use client";

import { CircleDashed, Gavel, GitMerge, GitPullRequest, GitPullRequestClosed, GitPullRequestCreate, Hourglass, Scale } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import type { components } from "@/lib/api/schema";

import { type StatusLook, StatusPill, type StatusPillProps } from "./status-badge";

/**
 * The state of what accepting a proposal made, as a pill. Only a proposal's page and the Inbox's sheet of a proposal
 * show one, so its map and icons live here rather than in `STATUS_LOOKS`, which nearly every page loads; the pill is
 * the same `StatusPill`, with its words under `status.change` in messages/en.json and vi.json.
 */

export type ChangeStatus = components["schemas"]["Change"]["state"];

/**
 * What an accepted proposal became (`judge.CHANGE_STATES`): planned waits for the night shift's Builder, the pull
 * request and the Judge move it on, and open waits for its owner in `attention`, with the reason the hub kept.
 */
export const CHANGE_LOOKS = {
  planned: { tone: "neutral", icon: CircleDashed },
  pr_pending: { tone: "running", icon: GitPullRequestCreate },
  judge_pending: { tone: "review", icon: Hourglass },
  judging: { tone: "review", icon: Scale },
  judged: { tone: "review", icon: Gavel },
  merged: { tone: "success", icon: GitMerge },
  open: { tone: "attention", icon: GitPullRequest },
  closed: { tone: "outline", icon: GitPullRequestClosed },
} as const satisfies Record<ChangeStatus, StatusLook>;

/** A change's state as a pill, found by the e2e specs as `change-state`. */
export function ChangeBadge({ status, label, ...props }: StatusPillProps & { status: ChangeStatus; label?: ReactNode }) {
  const t = useTranslations("status.change");
  return (
    <StatusPill
      look={CHANGE_LOOKS[status]}
      label={label ?? t(status)}
      data-kind="change"
      data-status={status}
      data-testid="change-state"
      {...props}
    />
  );
}
