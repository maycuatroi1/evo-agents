"use client";

import { CircleCheck, CirclePlay, CircleX, MessageSquare } from "lucide-react";
import { useTranslations } from "next-intl";

import { useMoney } from "@/components/data/facts";
import { type StatusLook, StatusPill, TONE_TEXT } from "@/components/status/status-badge";
import type { WorkBrief, WorkStatus } from "@/lib/work";
import { cn } from "@/lib/utils";

/**
 * How a work's state looks (web/DESIGN.md, Work card): running in `running` (cobalt), waiting for a person in
 * `attention` (amber), failed in `danger`, done in `success`. Colour is only ever a state's, and always with its word
 * or its icon; nothing pulses, since the chat says it is live only when its stream drops.
 */
export const WORK_LOOK = {
  running: { tone: "running", icon: CirclePlay, dot: "static" },
  needs_person: { tone: "attention", icon: MessageSquare },
  failed: { tone: "danger", icon: CircleX },
  done: { tone: "success", icon: CircleCheck },
} as const satisfies Record<WorkStatus, StatusLook>;

export function useWorkStatusText() {
  const t = useTranslations("chats.work.status");
  return (status: WorkStatus) => t(status);
}

export function WorkStatusPill({ status, size, testId = "work-status" }: { status: WorkStatus; size?: "default" | "lg"; testId?: string }) {
  const text = useWorkStatusText();
  return <StatusPill look={WORK_LOOK[status]} label={text(status)} size={size} data-status={status} data-testid={testId} />;
}

/** The state as a dot, where a row has no room for a pill: hidden from assistive technology, so its row says it too. */
export function WorkDot({ status, className }: { status: WorkStatus; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn("inline-flex size-2 shrink-0 rounded-full bg-current", TONE_TEXT[WORK_LOOK[status].tone], className)}
      data-testid="work-dot"
      data-status={status}
    />
  );
}

/** What the work waits for, in a line: the decision and who it asks, the person, the agent at work, or its end. */
export function useWaitingLine() {
  const t = useTranslations("chats.work.waiting");
  return (work: Pick<WorkBrief, "status" | "agent" | "waiting">): string => {
    const waiting = work.waiting;
    if (waiting?.kind === "decision") return waiting.login ? t("decision", { login: waiting.login }) : t("decisionAny");
    if (waiting?.kind === "person") return waiting.login ? t("person", { login: waiting.login }) : t("personAny");
    if (waiting?.kind === "run") return waiting.agent ? t("run", { agent: waiting.agent }) : t("runAny");
    if (work.status === "done") return t("done");
    if (work.status === "failed") return t("failed");
    return work.agent ? t("atWork", { agent: work.agent }) : t("nothing");
  };
}

/** A work's time, from its start to its end or to now: "2 h 5 min", "12 min", "40 s". */
export function useWorkTime() {
  const t = useTranslations("chats.work.time");
  return (seconds: number) => {
    const whole = Math.max(0, Math.round(seconds));
    const hours = Math.floor(whole / 3600);
    const minutes = Math.floor((whole % 3600) / 60);
    if (hours > 0) return t("hours", { hours, minutes });
    return minutes > 0 ? t("minutes", { minutes }) : t("seconds", { seconds: whole });
  };
}

/** The totals of a work in one line: "3 runs, $0.42, 12 min". */
export function useWorkTotals() {
  const t = useTranslations("chats.work");
  const money = useMoney();
  const time = useWorkTime();
  return (work: { runs: number; usd: number; seconds: number }) => t("totals", { runs: work.runs, cost: money(work.usd), time: time(work.seconds) });
}

/** A work chat's line in its row: its state as a dot and in words, then what it waits for. */
export function WorkLine({ work }: { work: WorkBrief }) {
  const t = useTranslations("chats.work.list");
  const statusText = useWorkStatusText();
  const waitingLine = useWaitingLine();
  return (
    <>
      <WorkDot status={work.status} className="mr-1.5 align-middle" />
      <span data-testid="work-row-line">{t("line", { status: statusText(work.status), waiting: waitingLine(work) })}</span>
    </>
  );
}
