"use client";

import { Bot, ChevronDown, CircleCheck, CircleSlash, CircleX, ListTree, type LucideIcon, SquareTerminal, Wrench } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useDeferredValue, useId, useMemo, useState } from "react";

import { useNow } from "@/hooks/use-now";
import { cn } from "@/lib/utils";

import type { RunEvent } from "./queries";
import { type RunTask, runTasks, type TaskKind, type TaskRow, taskBoard, type TaskStatus } from "./run-tasks";
import { useTraceDuration } from "./trace-look";

const KIND_ICON: Record<TaskKind, LucideIcon> = { agent: Bot, command: SquareTerminal, other: Wrench };
const END_ICON: Record<Exclude<TaskStatus, "running">, { icon: LucideIcon; tone: string }> = {
  completed: { icon: CircleCheck, tone: "text-success" },
  failed: { icon: CircleX, tone: "text-danger" },
  stopped: { icon: CircleSlash, tone: "text-muted-foreground" },
};
/** Finished tasks listed before "Show all". */
const FINISHED_SHOWN = 8;

const ROW =
  "group/task flex w-full min-w-0 items-start gap-2.5 px-3 text-left transition-colors duration-[var(--transition-duration-fast)] focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring max-md:min-h-11";
const ROW_LINK = "cursor-pointer hover:bg-accent";

function Elapsed({ task, now }: { task: RunTask; now: number | null }) {
  const short = useTraceDuration();
  const end = task.endedAt ? Date.parse(task.endedAt) : now;
  if (end === null) return null;
  return (
    <span
      className={cn("mt-0.5 shrink-0 font-mono text-xs leading-4 tabular-nums", task.status === "running" ? "text-running" : "text-fg-subtle")}
      data-testid="run-task-elapsed"
    >
      {short(Math.max(0, end - Date.parse(task.startedAt)))}
    </span>
  );
}

/** A task's row: a button that shows the call that started it in the trace, or plain text when no call is known. */
function TaskButton({ task, onShow, className, children }: { task: RunTask; onShow: (call: string) => void; className?: string; children: ReactNode }) {
  const t = useTranslations("runs.detail.tasks");
  const call = task.callId;
  if (!call) return <div className={cn(ROW, className)}>{children}</div>;
  return (
    <button type="button" onClick={() => onShow(call)} className={cn(ROW, ROW_LINK, className)} data-testid="run-task-show">
      {children}
      <ListTree
        className="mt-0.5 size-4 shrink-0 text-fg-subtle opacity-0 transition-opacity duration-[var(--transition-duration-fast)] group-hover/task:opacity-100 group-focus-visible/task:opacity-100 max-md:hidden"
        aria-hidden="true"
      />
      <span className="sr-only">{t("show")}</span>
    </button>
  );
}

function Tags({ task }: { task: RunTask }) {
  const t = useTranslations("runs.detail.tasks");
  return (
    <>
      {task.agentType ? <span className="font-mono text-xs leading-5 text-fg-subtle">{task.agentType}</span> : null}
      {task.background ? <span className="text-xs leading-5 text-muted-foreground">{t("background")}</span> : null}
    </>
  );
}

function RunningRow({ row, now, onShow }: { row: TaskRow; now: number | null; onShow: (call: string) => void }) {
  const t = useTranslations("runs.detail.tasks");
  const format = useFormatter();
  const { task, children } = row;
  const Icon = KIND_ICON[task.kind];
  const figures = [
    task.toolUses !== null ? t("calls", { count: task.toolUses }) : null,
    task.tokens !== null ? t("tokens", { tokens: format.number(task.tokens, { notation: "compact", maximumFractionDigits: 1 }) }) : null,
  ].filter((part): part is string => part !== null);
  return (
    <li className="min-w-0" data-testid="run-task" data-kind={task.kind} data-task-id={task.id}>
      <TaskButton task={task} onShow={onShow} className="py-2">
        <Icon className="mt-0.5 size-4 shrink-0 text-running" aria-hidden="true" />
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
            <span className="min-w-0 truncate text-sm leading-5 font-medium text-foreground" data-testid="run-task-name">
              {task.name}
            </span>
            <Tags task={task} />
          </span>
          {task.kind === "agent" && (task.activity || figures.length) ? (
            <span className="flex min-w-0 items-baseline gap-3 text-xs leading-4">
              {task.activity ? (
                <span className="min-w-0 truncate font-mono text-fg-subtle" title={task.activity} data-testid="run-task-activity">
                  {task.lastTool ? `${task.lastTool}: ${task.activity}` : task.activity}
                </span>
              ) : null}
              {figures.length ? <span className="ml-auto shrink-0 text-muted-foreground tabular-nums max-sm:hidden">{figures.join(", ")}</span> : null}
            </span>
          ) : null}
        </span>
        <Elapsed task={task} now={now} />
      </TaskButton>
      {children.length ? (
        <ul className="mb-1.5 ml-[38px] flex flex-col border-l" aria-label={t("commandsOf", { name: task.name })} data-testid="run-task-children">
          {children.map((child) => {
            const ChildIcon = KIND_ICON[child.kind];
            return (
              <li key={child.id} className="min-w-0" data-testid="run-task" data-kind={child.kind} data-task-id={child.id}>
                <TaskButton task={child} onShow={onShow} className="py-1.5">
                  <ChildIcon className="mt-0.5 size-3.5 shrink-0 text-running" aria-hidden="true" />
                  <span className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-2">
                    <span className="min-w-0 truncate text-[13px] leading-[18px] text-foreground" data-testid="run-task-name">
                      {child.name}
                    </span>
                    <Tags task={child} />
                  </span>
                  <Elapsed task={child} now={now} />
                </TaskButton>
              </li>
            );
          })}
        </ul>
      ) : null}
    </li>
  );
}

function FinishedRow({ task, onShow }: { task: RunTask; onShow: (call: string) => void }) {
  const t = useTranslations("runs.detail.tasks");
  const end = END_ICON[task.status === "running" ? "stopped" : task.status];
  const Icon = end.icon;
  return (
    <li className="min-w-0" data-testid="run-task-finished" data-kind={task.kind} data-status={task.status} data-task-id={task.id}>
      <TaskButton task={task} onShow={onShow} className="py-1.5">
        <Icon className={cn("mt-0.5 size-4 shrink-0", end.tone)} aria-hidden="true" />
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
            <span className="sr-only">{t(`status.${task.status === "running" ? "stopped" : task.status}`)}</span>
            <span className="min-w-0 truncate text-[13px] leading-[18px] text-foreground">{task.name}</span>
            <Tags task={task} />
          </span>
          {task.summary && task.summary !== task.name ? (
            <span className="truncate text-xs leading-4 text-muted-foreground" title={task.summary}>
              {task.summary}
            </span>
          ) : null}
        </span>
        <Elapsed task={task} now={null} />
      </TaskButton>
    </li>
  );
}

/**
 * What the run has going besides its main thread, over its trace: the subagents it started (what each does now, its
 * calls and tokens so far, the commands it runs) and the commands left running in the background, each with its time
 * so far. A row shows the call that started it in the trace. Shown only while something runs (the page says the
 * unusual), with the subagents and background commands that ended folded under it. Runs whose runtime sends no task
 * events (Codex, opencode) never show it.
 */
export function RunTasksPanel({ events, working, onShow }: { events: readonly RunEvent[]; working: boolean; onShow: (call: string) => void }) {
  const t = useTranslations("runs.detail.tasks");
  const ids = useId();
  const deferred = useDeferredValue(events);
  const tasks = useMemo(() => runTasks(deferred), [deferred]);
  const board = useMemo(() => taskBoard(tasks, working), [tasks, working]);
  const [open, setOpen] = useState(true);
  const [finishedOpen, setFinishedOpen] = useState(false);
  const [all, setAll] = useState(false);
  const live = board.running.length > 0;
  const now = useNow(live && open);
  if (!live) return null;

  const counts = [
    board.agents ? t("agents", { count: board.agents }) : null,
    board.commands ? t("commands", { count: board.commands }) : null,
  ].filter((part): part is string => part !== null);
  const finished = all ? board.finished : board.finished.slice(0, FINISHED_SHOWN);
  return (
    <section aria-labelledby={`${ids}-title`} className="mt-3 mb-1 min-w-0 shrink-0 overflow-hidden rounded-lg border bg-card" data-testid="run-tasks">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={`${ids}-list`}
        onClick={() => setOpen((value) => !value)}
        className="group/head flex w-full cursor-pointer items-center gap-2.5 px-3 py-2 text-left transition-colors duration-[var(--transition-duration-fast)] hover:bg-accent focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring max-md:min-h-11"
        data-testid="run-tasks-toggle"
      >
        {/* The live dot of DESIGN.md, the one pulsing element of the panel. */}
        <span className="flex size-4 shrink-0 items-center justify-center" aria-hidden="true">
          <span className="relative inline-flex size-2 rounded-full bg-running">
            <span className="absolute inset-0 animate-live-ping rounded-full bg-running" />
          </span>
        </span>
        <span id={`${ids}-title`} className="text-[13px] leading-5 font-semibold text-foreground">
          {t("title")}
        </span>
        <span className="min-w-0 truncate text-xs leading-5 text-muted-foreground" data-testid="run-tasks-counts">
          {counts.join(", ")}
        </span>
        <ChevronDown
          className="ml-auto size-4 shrink-0 text-fg-subtle transition-transform duration-[var(--transition-duration-fast)] group-aria-expanded/head:rotate-180"
          aria-hidden="true"
        />
      </button>
      {open ? (
        <div id={`${ids}-list`} className="max-h-[min(40vh,20rem)] overflow-y-auto overscroll-contain border-t">
          <ul className="flex flex-col divide-y" aria-label={t("runningLabel")}>
            {board.running.map((row) => (
              <RunningRow key={row.task.id} row={row} now={now} onShow={onShow} />
            ))}
          </ul>
          {board.finished.length ? (
            <div className="border-t">
              <button
                type="button"
                aria-expanded={finishedOpen}
                onClick={() => setFinishedOpen((value) => !value)}
                className="group/fin flex w-full cursor-pointer items-center gap-2 px-3 py-1.5 text-left text-xs leading-5 text-muted-foreground transition-colors duration-[var(--transition-duration-fast)] hover:bg-accent hover:text-foreground focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring max-md:min-h-11"
                data-testid="run-tasks-finished-toggle"
              >
                <ChevronDown className="size-3.5 -rotate-90 transition-transform duration-[var(--transition-duration-fast)] group-aria-expanded/fin:rotate-0" aria-hidden="true" />
                {t("finished", { count: board.finished.length })}
              </button>
              {finishedOpen ? (
                <>
                  <ul className="flex flex-col pb-1" aria-label={t("finishedLabel")}>
                    {finished.map((task) => (
                      <FinishedRow key={task.id} task={task} onShow={onShow} />
                    ))}
                  </ul>
                  {board.finished.length > FINISHED_SHOWN && !all ? (
                    <button
                      type="button"
                      onClick={() => setAll(true)}
                      className="mb-1.5 ml-[38px] cursor-pointer rounded-xs text-xs text-brand underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-ring max-md:min-h-11"
                      data-testid="run-tasks-finished-all"
                    >
                      {t("showAll", { count: board.finished.length })}
                    </button>
                  ) : null}
                </>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
