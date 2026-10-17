"use client";

import { ArrowRight, Bot, MessageSquare, User } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useMemo } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useNow } from "@/hooks/use-now";
import { cn } from "@/lib/utils";

import type { RunEvent } from "./queries";
import { callFailed, callRunning, countCalls, groupByIntent, groupSpan, type NormalItem, type ToolGroup } from "./trace-groups";
import { buildTrace } from "./trace-model";
import { useTraceDuration } from "./trace-look";
import { INTENT_ICON, useGroupLabel } from "./trace-tools";

/** How many of the latest rows a phone's run page shows before "Open full trace". */
const PREVIEW_ROWS = 4;

type Shown = Extract<NormalItem, { type: "group" | "agent" | "ask" | "user" }>;

function GroupLine({ group, active }: { group: ToolGroup; active: boolean }) {
  const t = useTranslations("runs.detail.trace.group");
  const label = useGroupLabel();
  const short = useTraceDuration();
  const last = group.calls[group.calls.length - 1].tool;
  const running = active && callRunning(last);
  const now = useNow(running);
  const failed = group.calls.filter(({ tool }) => callFailed(tool)).length;
  const span = groupSpan(group);
  const { title, detail } = label(group);
  const Icon = INTENT_ICON[group.intent];
  return (
    <>
      <Icon className={cn("mt-0.5 size-4 shrink-0", running ? "text-running" : failed ? "text-danger" : "text-muted-foreground")} aria-hidden="true" />
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="text-[15px] leading-[22px] font-medium text-foreground">
          {title}
          {failed ? <span className="font-normal text-danger">{`, ${t("failed", { count: failed })}`}</span> : null}
        </span>
        {detail ? <span className="truncate font-mono text-xs leading-4 text-fg-subtle">{detail}</span> : null}
      </span>
      <span className={cn("mt-1 shrink-0 font-mono text-xs leading-4 tabular-nums", running ? "text-running" : "text-fg-subtle")}>
        {running ? (now !== null ? short(Math.max(0, now - Date.parse(group.calls[0].tool.startedAt))) : null) : span !== null ? short(span) : null}
      </span>
    </>
  );
}

function TextLine({ item }: { item: Exclude<Shown, ToolGroup> }) {
  const t = useTranslations("runs.detail.trace");
  const Icon = item.type === "agent" ? Bot : item.type === "ask" ? MessageSquare : User;
  const text = item.type === "ask" ? item.question : item.text;
  return (
    <>
      <Icon className={cn("mt-0.5 size-4 shrink-0", item.type === "agent" ? "text-brand" : item.type === "ask" ? "text-attention" : "text-foreground")} aria-hidden="true" />
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="text-[15px] leading-[22px] font-medium text-foreground">{item.type === "agent" ? t("agent") : item.type === "ask" ? t("askedYou") : t("you")}</span>
        <span className="line-clamp-2 text-[13px] leading-[18px] text-muted-foreground [overflow-wrap:anywhere]">{text.trim().split("\n")[0]}</span>
      </span>
    </>
  );
}

/**
 * A phone's view of the trace on the run's page: its latest rows, newest first, the same rows as the trace's normal
 * view (a group of calls as what it did, the agent's words, a question, a message), and a button that opens the trace
 * (an author run's Chat, `label`) on a screen of its own, the one scroll region for it. The live group's clock is the only thing that moves.
 * While the log is first read (`loading`) the rows are grey bars in their place, not "Nothing was recorded".
 */
export function TracePreview({
  events,
  active,
  loading,
  label,
  onOpen,
}: {
  events: readonly RunEvent[];
  active: boolean;
  loading: boolean;
  label?: string;
  onOpen: () => void;
}) {
  const t = useTranslations("runs.detail.preview");
  const id = useId();
  const items = useMemo(() => groupByIntent(buildTrace(events)), [events]);
  const calls = countCalls(items);
  const recent = useMemo(
    () =>
      items
        .filter((item): item is Shown => item.type === "group" || item.type === "agent" || item.type === "ask" || item.type === "user")
        .slice(-PREVIEW_ROWS)
        .reverse(),
    [items],
  );
  return (
    <section className="flex min-w-0 flex-col gap-2" aria-labelledby={id} aria-busy={(loading && recent.length === 0) || undefined} data-testid="run-trace-preview">
      <div className="flex items-baseline justify-between gap-3">
        <h2 id={id} className="text-[15px] leading-[22px] font-semibold">
          {t("title")}
        </h2>
        {calls > 0 ? <span className="text-[13px] leading-[18px] text-muted-foreground tabular-nums">{t("calls", { count: calls })}</span> : null}
      </div>
      {loading && recent.length === 0 ? (
        <ol className="flex flex-col divide-y" data-testid="run-trace-preview-loading">
          {Array.from({ length: PREVIEW_ROWS }, (_, index) => (
            <li key={index} className="flex items-start gap-2.5 py-2.5">
              <Skeleton className="mt-0.5 size-4 shrink-0 rounded-sm" />
              <div className="flex min-w-0 flex-1 flex-col gap-1.5 py-0.5">
                <Skeleton className="h-3.5 w-24" />
                <Skeleton className="h-3.5 w-full" />
                <Skeleton className="h-3.5 w-2/3" />
              </div>
            </li>
          ))}
        </ol>
      ) : recent.length === 0 ? (
        <p className="text-sm text-muted-foreground">{active ? t("waiting") : t("none")}</p>
      ) : (
        <ol className="flex flex-col divide-y">
          {recent.map((item) => (
            <li key={item.key} className="flex min-w-0 items-start gap-2.5 py-2.5" data-testid="run-trace-preview-row" data-type={item.type}>
              {item.type === "group" ? <GroupLine group={item} active={active} /> : <TextLine item={item} />}
            </li>
          ))}
        </ol>
      )}
      <Button type="button" variant="outline" className="h-11 w-full justify-between" onClick={onOpen} data-testid="run-open-trace">
        {label ?? (calls > 0 ? t("open") : t("openEmpty"))}
        <ArrowRight aria-hidden="true" />
      </Button>
    </section>
  );
}
