"use client";

import { useFormatter, useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { StatusPill, type StatusPillProps } from "@/components/status/status-badge";
import type { ItemKind, ItemState, LoopItem, ProjectGoal } from "@/lib/loop-queries";

import { GOAL_LOOKS, goalWord, KIND_ICONS, STATE_LOOKS, subjectOf } from "./model";

/** A work item's state as a pill, in its tone: one pill per work item. */
export function ItemStateBadge({ state, label, ...props }: StatusPillProps & { state: ItemState; label?: string }) {
  const t = useTranslations("loop.state");
  return <StatusPill look={STATE_LOOKS[state]} label={label ?? t(state)} data-status={state} data-testid="item-state" {...props} />;
}

/** What kind of work item it is, as the kit's tag: a name, never a state. */
export function KindTag({ kind }: { kind: ItemKind }) {
  const t = useTranslations("loop.kind");
  const Icon = KIND_ICONS[kind];
  return (
    <Tag data-kind={kind} data-testid="item-kind">
      <Icon aria-hidden="true" />
      {t(kind)}
    </Tag>
  );
}

/** A work item's kind as its icon alone, before a title that already names it ("Build UC ORD-1"). */
export function KindIcon({ kind }: { kind: ItemKind }) {
  const Icon = KIND_ICONS[kind];
  return <Icon className="size-4 shrink-0 text-fg-subtle" aria-hidden="true" data-kind={kind} data-testid="item-kind-icon" />;
}

/** A goal's status as a pill: active, paused, or closed as achieved, dropped or expired. */
export function GoalStatusBadge({ goal, ...props }: StatusPillProps & { goal: Pick<ProjectGoal, "status" | "close_reason"> }) {
  const t = useTranslations("loop.goalStatus");
  const word = goalWord(goal);
  return <StatusPill look={GOAL_LOOKS[word]} label={t(word)} data-status={word} data-testid="goal-status" {...props} />;
}

/** A work item's title: "Build UC ORD-1", or the kind alone while it is about nothing the reader may read. */
export function useItemTitle(): (item: Pick<LoopItem, "kind" | "goal" | "claim" | "use_case" | "plan">) => string {
  const t = useTranslations("loop.title");
  const kind = useTranslations("loop.kind");
  return (item) => {
    const subject = subjectOf(item);
    return subject ? t(item.kind, { subject }) : kind(item.kind);
  };
}

function text(value: unknown): string {
  if (Array.isArray(value)) return value.join(", ");
  return value === undefined || value === null ? "" : String(value);
}

function count(value: unknown): number {
  return typeof value === "number" ? value : 0;
}

/**
 * Why a work item stands where it does, in one line, from its reason and the figures of that reason: "Worker
 * northwind-mini: offline", "3 of 3 development runs at work". Null for a work item with no reason (running, done).
 */
export function useWaitingText(): (item: Pick<LoopItem, "reason" | "waiting" | "failures" | "task">) => string | null {
  const t = useTranslations("loop.reason");
  const cycle = useTranslations("loop.cycle");
  const format = useFormatter();
  return (item) => {
    const figures = item.waiting;
    switch (item.reason) {
      case null:
        return null;
      case "concurrency":
        return figures.repos
          ? t("concurrencyRepos", { repos: text(figures.repos), max: count(figures.repo_max_running) })
          : t("concurrency", {
              running: count(figures.running),
              max: count(figures.max_running),
              cycle: figures.cycle === "business" || figures.cycle === "development" ? cycle(figures.cycle) : "",
            });
      case "rate":
        return t("rate", { dispatched: count(figures.dispatched), max: count(figures.dispatches_per_hour) });
      case "worker":
        return figures.worker ? t("worker", { worker: text(figures.worker), why: text(figures.why) }) : t("workerNone", { why: text(figures.why) });
      case "type_refused":
        return t("type_refused", { why: text(figures.why) });
      case "stable":
        return typeof figures.stable_at === "string"
          ? t("stable", { at: format.dateTime(new Date(figures.stable_at), { dateStyle: "medium", timeStyle: "short" }) })
          : t("stableSoon");
      case "split":
        return t("split", { task: text(figures.task ?? item.task ?? "?") });
      case "triage":
      case "loop_decision":
        return t(item.reason, { failures: count(figures.failures) || item.failures });
      case "srs_finding":
        return t("srs_finding", { findings: count(figures.findings) });
      case "merge_blocked":
        return t("merge_blocked", { merges: count(figures.merges) });
      case "merge_manual":
        return figures.by_hand && text(figures.by_hand) ? t("merge_manual", { repos: text(figures.by_hand) }) : t("merge_manualAny");
      default:
        return t(item.reason);
    }
  };
}
