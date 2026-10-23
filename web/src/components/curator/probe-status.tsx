"use client";

import { CircleDashed, FileDiff, FlaskConicalOff, Repeat2 } from "lucide-react";
import { useTranslations } from "next-intl";

import { type StatusLook, StatusPill } from "@/components/status/status-badge";
import { cn } from "@/lib/utils";

import { useInsightFormat } from "./insight-format";
import { type FlaggedStatus, isFlagged } from "./probe-model";
import type { ProbeArmCount, ProbeStatus } from "./probe-queries";

/**
 * A probe's status as a pill, only when something is wrong: the mistake came back with the rule in place (`danger`),
 * the probe did not bring it back with the rule taken off, the change's diff no longer comes off the default branch,
 * or no probe run measured it. A probe that holds wears nothing, which is how the page says "as expected".
 */
export const PROBE_LOOKS = {
  recurring: { tone: "danger", icon: Repeat2 },
  not_reproduced: { tone: "neutral", icon: FlaskConicalOff },
  stale: { tone: "neutral", icon: FileDiff },
  unmeasured: { tone: "outline", icon: CircleDashed },
} as const satisfies Record<FlaggedStatus, StatusLook>;

/** The flag of a probe's status, found by the e2e specs as `probe-flag`; nothing for `holds`. */
export function ProbeFlag({ status, className }: { status: ProbeStatus; className?: string }) {
  const t = useTranslations("curator.probes.status");
  if (!isFlagged(status)) return null;
  return <StatusPill look={PROBE_LOOKS[status]} label={t(status)} className={className} data-testid="probe-flag" data-status={status} />;
}

/** One arm of a result: how many of its sessions repeated the mistake, of how many, and the recurrence the hub reckoned. */
export function ArmCount({ arm, side }: { arm: ProbeArmCount; side: "on" | "off" }) {
  const t = useTranslations("curator.probes");
  const format = useInsightFormat();
  if (arm.runs === 0) return <span className="text-fg-subtle" data-arm={side}>{t("notRun")}</span>;
  return (
    <span className="inline-flex flex-col items-end gap-0.5" data-arm={side} data-failed={arm.failed} data-runs={arm.runs}>
      <span className={cn(arm.failed > 0 && side === "on" ? "text-danger" : "text-foreground")}>{t("failed", { failed: arm.failed, runs: arm.runs })}</span>
      {arm.recurrence !== null ? <span className="text-xs text-fg-subtle">{t("recurrence", { share: format.percent(arm.recurrence) })}</span> : null}
    </span>
  );
}

/** A result's runtime and model: the model the charter names, or the runtime's own choice. */
export function ModelName({ runtime, model }: { runtime: string; model: string | null }) {
  const t = useTranslations("curator.probes");
  return (
    <span className="inline-flex min-w-0 flex-col">
      <span className={cn("text-xs [overflow-wrap:anywhere]", model === null ? "text-muted-foreground" : "font-mono text-foreground")}>{model ?? t("runtimeModel")}</span>
      <span className="font-mono text-xs text-fg-subtle">{runtime}</span>
    </span>
  );
}

/** What the week before said of a result, when it said something else, and why a stale or unmeasured one is so. */
export function ResultNotes({ status, previous, reason }: { status: ProbeStatus; previous: ProbeStatus | null; reason: string | null }) {
  const t = useTranslations("curator.probes");
  if ((previous === null || previous === status) && !reason) return null;
  return (
    <span className="flex min-w-0 flex-col gap-0.5 text-xs text-pretty text-fg-subtle">
      {previous !== null && previous !== status ? <span data-testid="probe-previous">{t("previous", { status: t(`word.${previous}`) })}</span> : null}
      {reason ? <span className="[overflow-wrap:anywhere]" data-testid="probe-reason">{reason}</span> : null}
    </span>
  );
}
