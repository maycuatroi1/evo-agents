"use client";

import {
  Archive,
  CircleCheck,
  CircleDashed,
  CircleDot,
  CircleHelp,
  CircleMinus,
  Flag,
  GitMerge,
  type LucideIcon,
  OctagonAlert,
  Rocket,
} from "lucide-react";
import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { type PlanArea, REPO_STATUSES, type RepoStatus, type StepGroup } from "@/lib/plans";
import { cn } from "@/lib/utils";

/**
 * Status, blocking and area as an icon and a word, never colour alone (web/DESIGN.md). The colours are the
 * design system's state tokens: success for done, info for in progress, warning for blocked, outline otherwise.
 */
type Variant = "outline" | "info" | "success" | "warning";

export const STEP_LOOK: Record<StepGroup, { icon: LucideIcon; variant: Variant; tone: string }> = {
  pending: { icon: CircleDashed, variant: "outline", tone: "text-muted-foreground" },
  in_progress: { icon: CircleDot, variant: "info", tone: "text-running" },
  blocked: { icon: OctagonAlert, variant: "warning", tone: "text-attention" },
  done: { icon: CircleCheck, variant: "success", tone: "text-success" },
  other: { icon: CircleHelp, variant: "outline", tone: "text-muted-foreground" },
};

/** The word for a step's status; `raw` is the status as written when it is none of the four. */
export function useStepStatusText() {
  const t = useTranslations("plans.status");
  return (group: StepGroup, raw: string | null = null) =>
    group === "other" ? t("other", { status: raw ?? "?" }) : t(group);
}

export function StepStatusBadge({
  group,
  raw = null,
  className,
}: {
  group: StepGroup;
  raw?: string | null;
  className?: string;
}) {
  const label = useStepStatusText();
  const look = STEP_LOOK[group];
  return (
    <Badge variant={look.variant} className={className} data-status={group}>
      <look.icon aria-hidden="true" />
      {label(group, raw)}
    </Badge>
  );
}

/** The status icon alone, with its word for screen readers (for dense lists where a badge would crowd). */
export function StepStatusIcon({ group, raw = null, className }: { group: StepGroup; raw?: string | null; className?: string }) {
  const label = useStepStatusText();
  const look = STEP_LOOK[group];
  return (
    <span className={cn("inline-flex shrink-0", look.tone, className)} title={label(group, raw)}>
      <look.icon className="size-4" aria-hidden="true" />
      <span className="sr-only">{label(group, raw)}</span>
    </span>
  );
}

/** A step marked `blocking: true` holds the plan back until it is done. */
export function BlockingBadge({ blocking, showFalse = false }: { blocking: boolean | null; showFalse?: boolean }) {
  const t = useTranslations("plans.step");
  if (blocking !== true && !showFalse) return null;
  if (blocking === null) return <span className="text-sm text-muted-foreground">{t("blockingUnset")}</span>;
  return (
    <Badge variant="outline" data-blocking={blocking ? "true" : "false"}>
      {blocking ? <Flag aria-hidden="true" /> : <CircleMinus aria-hidden="true" />}
      {blocking ? t("blocking") : t("notBlocking")}
    </Badge>
  );
}

const REPO_LOOK: Record<RepoStatus, { icon: LucideIcon; variant: Variant }> = {
  merged: { icon: GitMerge, variant: "success" },
  done: { icon: CircleCheck, variant: "success" },
  in_progress: { icon: CircleDot, variant: "info" },
  pending: { icon: CircleDashed, variant: "outline" },
  "not-needed": { icon: CircleMinus, variant: "outline" },
};

function isRepoStatus(status: string): status is RepoStatus {
  return (REPO_STATUSES as readonly string[]).includes(status);
}

export function RepoStatusBadge({ status }: { status: string | null }) {
  const t = useTranslations("plans.repoStatus");
  if (status === null) return <span className="text-muted-foreground">-</span>;
  const known = isRepoStatus(status);
  const look = known ? REPO_LOOK[status] : { icon: CircleHelp, variant: "outline" as const };
  return (
    <Badge variant={look.variant}>
      <look.icon aria-hidden="true" />
      {known ? t(status) : t("other", { status })}
    </Badge>
  );
}

export function AreaBadge({ area }: { area: PlanArea }) {
  const t = useTranslations("plans.area");
  const Icon = area === "completed" ? Archive : Rocket;
  return (
    <Badge variant={area === "completed" ? "success" : "info"} data-area={area}>
      <Icon aria-hidden="true" />
      {t(area)}
    </Badge>
  );
}
