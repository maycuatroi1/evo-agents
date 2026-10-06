"use client";

import { CircleMinus, Flag } from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import {
  LookIcon,
  OTHER_LOOK,
  OtherStatusBadge,
  STATUS_LOOKS,
  StatusBadge,
  StatusIcon,
  type StatusLook,
  useStatusText,
} from "@/components/status/status-badge";
import { REPO_STATUSES, type RepoStatus, type StepGroup } from "@/lib/plans";

/**
 * A plan's steps and repos on the shared `StatusBadge` (web/DESIGN.md): what the plan pages add is a status the hub
 * does not know (`other`, kept as written) and the blocking flag.
 */

/** A step group's look; `other` is a question mark in an outline. */
export function stepLook(group: StepGroup): StatusLook {
  return group === "other" ? OTHER_LOOK : STATUS_LOOKS.step[group];
}

/** The word for a step's status; `raw` is the status as written when it is none of the four. */
export function useStepStatusText() {
  const text = useStatusText("step");
  const t = useTranslations("status");
  return (group: StepGroup, raw: string | null = null) =>
    group === "other" ? t("other", { status: raw ?? "?" }) : text(group);
}

export function StepStatusBadge({ group, raw = null, className }: { group: StepGroup; raw?: string | null; className?: string }) {
  return group === "other" ? (
    <OtherStatusBadge raw={raw} className={className} />
  ) : (
    <StatusBadge kind="step" status={group} className={className} />
  );
}

/** The status icon alone, with its word for screen readers (for dense lists where a pill would crowd). */
export function StepStatusIcon({ group, raw = null, className }: { group: StepGroup; raw?: string | null; className?: string }) {
  const label = useStepStatusText();
  return group === "other" ? (
    <LookIcon look={OTHER_LOOK} label={label(group, raw)} className={className} data-status="other" />
  ) : (
    <StatusIcon kind="step" status={group} className={className} />
  );
}

/** A step marked `blocking: true` holds the plan back until it is done: a property of the step, so a tag. */
export function BlockingBadge({ blocking, showFalse = false }: { blocking: boolean | null; showFalse?: boolean }) {
  const t = useTranslations("plans.step");
  if (blocking !== true && !showFalse) return null;
  if (blocking === null) return <span className="text-sm text-muted-foreground">{t("blockingUnset")}</span>;
  return (
    <Tag data-blocking={blocking ? "true" : "false"}>
      {blocking ? <Flag aria-hidden="true" /> : <CircleMinus aria-hidden="true" />}
      {blocking ? t("blocking") : t("notBlocking")}
    </Tag>
  );
}

function isRepoStatus(status: string): status is RepoStatus {
  return (REPO_STATUSES as readonly string[]).includes(status);
}

export function RepoStatusBadge({ status }: { status: string | null }) {
  if (status === null) return <span className="text-muted-foreground">-</span>;
  return isRepoStatus(status) ? <StatusBadge kind="repo" status={status} /> : <OtherStatusBadge raw={status} />;
}
