"use client";

import { CircleCheck, CircleHelp, CircleX, Hand, LoaderCircle } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { StatusPill, type StatusLook } from "@/components/status/status-badge";
import { agentName, durationParts, type WorkStatus } from "@/lib/systems-work";
import type { TraceSummary } from "@/lib/systems";

/**
 * The status of a work on the Observe page (web/DESIGN.md, Observe: work and timeline), as a pill beside the one page
 * that shows it: running with the pulsing dot, needing a person in `attention`, failed, done. A trace an older hub
 * wrote without a status is Unknown, in outline.
 */
const WORK_LOOK: Record<WorkStatus | "unknown", StatusLook> = {
  running: { tone: "running", icon: LoaderCircle, dot: "live" },
  needs_person: { tone: "attention", icon: Hand },
  failed: { tone: "danger", icon: CircleX },
  done: { tone: "success", icon: CircleCheck },
  unknown: { tone: "outline", icon: CircleHelp },
};

export function WorkStatusPill({ status }: { status: TraceSummary["status"] }) {
  const t = useTranslations("systems.observe.work.status");
  const known = status ?? "unknown";
  return <StatusPill look={WORK_LOOK[known]} label={t(known)} data-testid="system-work-status" data-status={known} />;
}

/** A time as "1h 5m", "8m 12s", "42s". */
export function useWorkDuration() {
  const t = useTranslations("systems.observe.work.duration");
  return (seconds: number) => {
    const { hours, minutes, seconds: rest } = durationParts(seconds);
    if (hours > 0) return t("hours", { hours, minutes });
    if (minutes > 0) return t("minutes", { minutes, seconds: rest });
    return t("seconds", { seconds: rest });
  };
}

/** A sum in dollars, four decimals at most. */
export function useUsd() {
  const format = useFormatter();
  return (usd: number) => format.number(usd, { style: "currency", currency: "USD", maximumFractionDigits: 4 });
}

/** What a work waits for, in words: an agent's answer, a person at a run, a person's decision; null for nothing. */
export function useWaitingText(names: ReadonlyMap<string, string>) {
  const t = useTranslations("systems.observe.work");
  return (waiting: TraceSummary["waiting"]): string | null => {
    if (!waiting) return null;
    if (waiting.kind === "decision") return t("waitsDecision", { login: waiting.login ? `@${waiting.login}` : t("timeline.someone"), question: waiting.question ?? "" });
    if (waiting.kind === "person") return t("waitsPerson", { login: waiting.login ? `@${waiting.login}` : t("timeline.someone") });
    return t("waitsRun", { agent: agentName(waiting.agent, names) });
  };
}
