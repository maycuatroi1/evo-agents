"use client";

import { ArrowRight, CircleCheck, CircleSlash, CircleX, LoaderCircle, type LucideIcon, Users } from "lucide-react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { useNow } from "@/hooks/use-now";
import { cn } from "@/lib/utils";

import { briefLines, type RunTask, type TaskStatus, taskOf } from "./run-tasks";
import type { Delegation } from "./trace-groups";
import { useTraceDuration } from "./trace-look";
import { Row, Who } from "./trace-row";
import type { TraceContext } from "./trace-rows";
import type { ToolCall } from "./trace-model";

/**
 * Where the main agent hands work to subagents (trace-groups.ts): one card for the whole handing, a line per subagent
 * with its name, its type, the first lines of its brief, its report's first lines or what it does now, and a button
 * that opens its profile (agent-profile.tsx), where its brief, its own activity and its whole report are. The subagent's
 * own calls are not in the main agent's trace: they are its profile's Activity.
 */

const STATUS_ICON: Record<TaskStatus, { icon: LucideIcon; tone: string }> = {
  running: { icon: LoaderCircle, tone: "animate-spin text-running motion-reduce:animate-none" },
  completed: { icon: CircleCheck, tone: "text-success" },
  failed: { icon: CircleX, tone: "text-danger" },
  stopped: { icon: CircleSlash, tone: "text-muted-foreground" },
};

/** A call's status as a task's, for a call no task names (the page passes the run's tasks). */
function statusOfCall(tool: ToolCall): TaskStatus {
  if (tool.status === "failed") return "failed";
  if (tool.status === "completed") return "completed";
  return "running";
}

function DelegatedAgent({ tool, task, context }: { tool: ToolCall; task: RunTask | null; context: TraceContext }) {
  const t = useTranslations("runs.detail.trace.delegation");
  const short = useTraceDuration();
  const status = task ? (context.active || task.status !== "running" ? task.status : "stopped") : statusOfCall(tool);
  const running = status === "running";
  const now = useNow(running);
  const startedAt = task?.startedAt ?? tool.startedAt;
  const end = task?.endedAt ?? tool.endedAt;
  const elapsed = end ? Date.parse(end) - Date.parse(startedAt) : now !== null ? now - Date.parse(startedAt) : null;
  const look = STATUS_ICON[status];
  const Icon = look.icon;
  const name = task?.name ?? tool.description ?? tool.arg ?? t("unnamed");
  const brief = briefLines(task?.prompt ?? null);
  const result = task?.result ? briefLines(task.result, 2, 200) : [];
  const id = task?.id ?? tool.id;
  return (
    <li className="flex min-w-0 flex-col gap-2 px-3 py-2.5" data-testid="trace-delegation-agent" data-task-id={id ?? undefined} data-tool-call-id={tool.id ?? undefined} data-status={status}>
      <div className="flex min-w-0 items-start gap-2">
        <Icon className={cn("mt-0.5 size-4 shrink-0", look.tone)} aria-hidden="true" />
        <span className="sr-only">{t(`status.${status}`)}</span>
        <span className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className="min-w-0 text-sm leading-5 font-medium text-foreground [overflow-wrap:anywhere]" data-testid="trace-delegation-name">
            {name}
          </span>
          {task?.agentType ? <span className="font-mono text-xs leading-5 text-fg-subtle">{task.agentType}</span> : null}
          {task?.background ? <span className="text-xs leading-5 text-muted-foreground">{t("background")}</span> : null}
        </span>
        {elapsed !== null ? (
          <span className={cn("mt-0.5 shrink-0 font-mono text-xs leading-4 tabular-nums", running ? "text-running" : "text-fg-subtle")}>{short(Math.max(0, elapsed))}</span>
        ) : null}
      </div>
      {brief.length ? (
        <p className="line-clamp-3 text-[15px] leading-6 whitespace-pre-line text-muted-foreground [overflow-wrap:anywhere]" data-testid="trace-delegation-brief">
          {brief.join("\n")}
        </p>
      ) : null}
      <div className="flex min-w-0 flex-wrap items-end justify-between gap-x-3 gap-y-2">
        <p className="min-w-0 flex-1 text-[15px] leading-6 [overflow-wrap:anywhere]" data-testid="trace-delegation-outcome">
          {result.length ? (
            <>
              <span className="font-medium text-foreground">{t("reported")} </span>
              <span className="line-clamp-2 whitespace-pre-line text-foreground">{result.join("\n")}</span>
            </>
          ) : running ? (
            <span className="text-running">{task?.activity ? t("doing", { activity: task.lastTool ? `${task.lastTool}: ${task.activity}` : task.activity }) : t("working")}</span>
          ) : (
            <span className="text-muted-foreground">{t("noReport")}</span>
          )}
        </p>
        {id && context.openAgent ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="shrink-0 max-md:min-h-11"
            onClick={() => context.openAgent?.(id)}
            aria-label={t("openLabel", { name })}
            data-testid="trace-delegation-open"
          >
            {t("open")}
            <ArrowRight aria-hidden="true" />
          </Button>
        ) : null}
      </div>
    </li>
  );
}

/** The card of one handing of work: "Handed work to 2 sub-agents", then a line per subagent. */
export function DelegationRow({ item, tasks, context }: { item: Delegation; tasks: readonly RunTask[]; context: TraceContext }) {
  const t = useTranslations("runs.detail.trace.delegation");
  return (
    <Row icon={Users} look="agent" at={item.at} head={<Who>{t("head", { count: item.calls.length })}</Who>}>
      <ul className="flex min-w-0 flex-col divide-y overflow-hidden rounded-lg border bg-card" aria-label={t("label", { count: item.calls.length })} data-testid="trace-delegation">
        {item.calls.map(({ key, tool }) => (
          <DelegatedAgent key={key} tool={tool} task={taskOf(tasks, tool.id)} context={context} />
        ))}
      </ul>
    </Row>
  );
}
