"use client";

import { Hourglass } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import type { ComponentProps } from "react";

import { spanParts, type WaitSubject } from "@/lib/waits";

import { StatusPill } from "./status-badge";

/**
 * What a run waits for as a neutral pill (web/DESIGN.md, The wait panel): "Waiting on deploy 6/10", "Waiting on
 * northwind-launch step 8", "Waiting on @ea-researcher", "Waiting until 09:00". A wait asks nobody anything, so the pill
 * is never `attention`: a person is not needed. The run's header, its banner, Home's In flight and the Inbox's group of
 * waits show it.
 */
export function WaitPill({ subject, size, className, ...props }: { subject: WaitSubject; size?: "default" | "lg"; className?: string } & Omit<ComponentProps<"span">, "children">) {
  const label = useWaitLabel()(subject);
  return (
    <StatusPill
      look={{ tone: "neutral", icon: Hourglass }}
      label={label}
      size={size}
      className={className}
      title={label}
      data-testid="wait-pill"
      data-wait={subject.kind}
      {...props}
    />
  );
}

/** The words of a wait's pill, for a subject (`waitSubject`). */
export function useWaitLabel() {
  const t = useTranslations("wait.pill");
  const format = useFormatter();
  return (subject: WaitSubject): string => {
    switch (subject.kind) {
      case "workflow_run":
        return subject.total > 0 ? t("workflow", { workflow: subject.workflow, done: subject.done, total: subject.total }) : t("workflowNoSteps", { workflow: subject.workflow });
      case "plan_step":
        return t("planStep", { plan: subject.plan, step: subject.step });
      case "agent_answer":
        return subject.handle ? t("agent", { handle: subject.handle }) : subject.run !== null ? t("agentRun", { run: subject.run }) : t("unknown");
      case "merge_check":
        return t("mergeCheck", { number: subject.number });
      case "time":
        return subject.at ? t("time", { time: format.dateTime(new Date(subject.at), { timeStyle: "short" }) }) : t("unknown");
      case "event":
        return t("event", { name: subject.name });
      case "reason":
        return t("reason", { reason: subject.reason });
      default:
        return t("unknown");
    }
  };
}

/**
 * A span of time in the wait's words: "45s", "4m 12s", "1 h 29 min". The seconds show under an hour, where a build's
 * time moves by them.
 */
export function useWaitSpan() {
  const t = useTranslations("wait.span");
  return (spanMs: number): string => {
    const { hours, minutes, seconds } = spanParts(spanMs);
    if (hours > 0) return t("hours", { hours, minutes });
    return minutes > 0 ? t("minutes", { minutes, seconds }) : t("seconds", { seconds });
  };
}
