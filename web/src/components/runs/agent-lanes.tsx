"use client";

import { Bot, Brain, ChevronDown, CircleCheck, CircleSlash, CircleX, type LucideIcon, SquareTerminal } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type CSSProperties, type ReactNode, useId, useMemo, useState } from "react";

import { useNow } from "@/hooks/use-now";
import { cn } from "@/lib/utils";

import { buildLanes, type Lane, type LaneScope, type LaneStatus, type Segment } from "./agent-lanes-model";
import type { RunEvent } from "./queries";
import type { RunTask } from "./run-tasks";
import { useTraceDuration } from "./trace-look";

/** Finished sub-agent lanes shown before they fold under one line. */
const FINISHED_SHOWN = 3;

const AVATAR: Record<Lane["kind"], LucideIcon> = { main: Brain, agent: Bot, commands: SquareTerminal };
const STATUS_ICON: Partial<Record<LaneStatus, { icon: LucideIcon; tone: string }>> = {
  completed: { icon: CircleCheck, tone: "text-success" },
  failed: { icon: CircleX, tone: "text-danger" },
  stopped: { icon: CircleSlash, tone: "text-muted-foreground" },
};

const ROW =
  "group/lane grid w-full min-w-0 items-center gap-x-4 gap-y-2 px-3 py-2 text-left max-md:min-h-16 md:grid-cols-[minmax(0,15rem)_minmax(0,1fr)] md:py-1.5";
const ROW_LINK =
  "cursor-pointer transition-colors duration-[var(--transition-duration-fast)] hover:bg-accent focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring";

/** Where a time falls on the lanes' shared axis, in percent. */
const pct = (at: number, from: number, to: number) => `${(Math.min(Math.max((at - from) / (to - from), 0), 1) * 100).toFixed(3)}%`;

function Bar({ segment, lane, from, to }: { segment: Segment; lane: Lane; from: number; to: number }) {
  const left = pct(segment.from, from, to);
  const width = `max(3px, ${((Math.min(segment.to, to) - Math.max(segment.from, from)) / (to - from)) * 100}%)`;
  const failed = lane.status === "failed";
  const look =
    segment.kind === "wait"
      ? "border border-dashed border-fg-subtle bg-transparent"
      : segment.running
        ? "bg-running"
        : failed
          ? "bg-danger"
          : lane.status === "completed" || lane.kind === "main"
            ? "bg-fg-subtle"
            : "bg-muted-foreground";
  return (
    <span
      className={cn("absolute inset-y-1.5 rounded-xs", look)}
      style={{ left, width } satisfies CSSProperties}
      data-segment={segment.kind}
      data-testid={segment.kind === "wait" ? "lane-wait" : "lane-work"}
    />
  );
}

function Track({ lane, from, to }: { lane: Lane; from: number; to: number }) {
  const t = useTranslations("runs.detail.agents");
  const short = useTraceDuration();
  const name = lane.kind === "main" ? t("main") : lane.kind === "commands" ? t("commands") : (lane.name ?? "");
  const sum = (kind: Segment["kind"]) => lane.segments.filter((s) => s.kind === kind).reduce((total, s) => total + (s.to - s.from), 0);
  const label =
    lane.kind === "main"
      ? t("track", { name, work: short(sum("work")), wait: short(sum("wait")) })
      : lane.kind === "agent"
        ? t("trackAgent", { name, work: short(sum("work")) })
        : t("trackCommands", { name, count: lane.count });
  return (
    <span className="relative block h-6 min-w-0 rounded-xs bg-muted/60 max-md:h-5" role="img" aria-label={label} data-testid="lane-track">
      {lane.segments.map((segment, i) => (
        <Bar key={i} segment={segment} lane={lane} from={from} to={to} />
      ))}
      {lane.marks.map((mark, i) =>
        mark.kind === "step" ? (
          <span
            key={i}
            className="absolute inset-y-0 w-0.5 bg-foreground"
            style={{ left: pct(mark.at, from, to) }}
            title={t("stepDone", { step: mark.agent })}
            data-testid="lane-step"
            data-step={mark.agent}
          >
            <span className="absolute top-0 right-1 rounded-xs bg-card px-1 text-xs leading-4 font-medium text-foreground tabular-nums" aria-hidden="true">
              {mark.agent}
            </span>
          </span>
        ) : (
          <span
            key={i}
            className={cn("absolute top-1/2 size-2.5 -translate-x-1/2 -translate-y-1/2 rotate-45", mark.kind === "handoff" ? "bg-brand" : "border-2 border-brand bg-card")}
            style={{ left: pct(mark.at, from, to) }}
            title={mark.kind === "handoff" ? t("handoff", { agent: mark.agent }) : t("returned", { agent: mark.agent })}
            data-testid={mark.kind === "handoff" ? "lane-handoff" : "lane-return"}
          />
        ),
      )}
    </span>
  );
}

function Who({ lane, waitingFor }: { lane: Lane; waitingFor: string | null }) {
  const t = useTranslations("runs.detail.agents");
  const tTask = useTranslations("runs.detail.tasks");
  const format = useFormatter();
  const Avatar = AVATAR[lane.kind];
  const end = STATUS_ICON[lane.status];
  const EndIcon = end?.icon;
  const name = lane.kind === "main" ? t("main") : lane.kind === "commands" ? t("commands") : (lane.name ?? "");
  const task = lane.task;
  const figures = [
    task?.toolUses != null ? t("calls", { count: task.toolUses }) : null,
    task?.tokens != null ? t("tokens", { tokens: format.number(task.tokens, { notation: "compact", maximumFractionDigits: 1 }) }) : null,
  ].filter((part): part is string => part !== null);
  const status = lane.kind === "commands" ? t("commandCount", { live: lane.live, count: lane.count }) : t(`status.${lane.status}`);
  const work = lane.status === "waiting" && waitingFor ? t("waitingFor", { names: waitingFor }) : lane.work;
  return (
    <span className="flex min-w-0 items-center gap-3">
      <span
        className={cn("flex size-8 shrink-0 items-center justify-center rounded-full bg-muted", lane.status === "running" || lane.status === "working" ? "text-running" : "text-muted-foreground")}
        aria-hidden="true"
      >
        <Avatar className="size-4" />
      </span>
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="flex min-w-0 items-baseline gap-2">
          <span className="min-w-0 truncate text-sm leading-5 font-medium text-foreground" title={name} data-testid="lane-name">
            {name}
          </span>
          {task?.agentType ? <span className="shrink-0 font-mono text-xs leading-5 text-fg-subtle">{task.agentType}</span> : null}
          {task?.background ? <span className="shrink-0 text-xs leading-5 text-muted-foreground">{tTask("background")}</span> : null}
        </span>
        <span className="flex min-w-0 items-center gap-1.5 text-xs leading-4 text-muted-foreground" data-testid="lane-status" data-status={lane.status}>
          {EndIcon ? <EndIcon className={cn("size-3.5 shrink-0", end.tone)} aria-hidden="true" /> : null}
          <span className="min-w-0 truncate">{figures.length && lane.kind === "agent" ? `${status}, ${figures.join(", ")}` : status}</span>
        </span>
        {work ? (
          <span className={cn("truncate text-xs leading-4 text-fg-subtle", lane.kind !== "main" && lane.status === "running" && "font-mono")} title={work} data-testid="lane-work-now">
            {work}
          </span>
        ) : null}
      </span>
    </span>
  );
}

function LaneRow({ lane, from, to, waitingFor, onOpen }: { lane: Lane; from: number; to: number; waitingFor: string | null; onOpen: (lane: Lane) => void }) {
  const t = useTranslations("runs.detail.agents");
  const openable = lane.kind !== "commands" || lane.call !== null;
  const body = (
    <>
      <Who lane={lane} waitingFor={waitingFor} />
      <Track lane={lane} from={from} to={to} />
    </>
  );
  const props = { "data-testid": "agent-lane", "data-lane": lane.id, "data-kind": lane.kind, "data-status": lane.status };
  if (!openable) return <li className="min-w-0"><div className={ROW} {...props}>{body}</div></li>;
  return (
    <li className="min-w-0">
      <button type="button" onClick={() => onOpen(lane)} className={cn(ROW, ROW_LINK)} {...props}>
        {body}
        <span className="sr-only">{lane.kind === "main" ? t("openMain") : lane.kind === "commands" ? t("openCommand") : t("open")}</span>
      </button>
    </li>
  );
}

function RangeButton({ pressed, onClick, children, id }: { pressed: boolean; onClick: () => void; children: ReactNode; id: string }) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={onClick}
      className={cn(
        "cursor-pointer rounded-md px-2.5 py-1 text-xs leading-5 font-medium transition-colors duration-[var(--transition-duration-fast)] focus-visible:outline-2 focus-visible:outline-ring max-md:min-h-11 max-md:px-3",
        pressed ? "bg-foreground text-background" : "text-muted-foreground hover:bg-accent hover:text-foreground",
      )}
      data-testid={id}
    >
      {children}
    </button>
  );
}

function Legend({ steps }: { steps: boolean }) {
  const t = useTranslations("runs.detail.agents");
  return (
    <ul className="flex flex-wrap justify-center gap-x-4 gap-y-1 text-xs leading-4 text-muted-foreground max-md:order-last max-md:w-full max-md:justify-start" aria-label={t("legend")}>
      <li className="flex items-center gap-1.5">
        <span className="h-2 w-4 rounded-xs bg-fg-subtle" aria-hidden="true" />
        {t("legendWork")}
      </li>
      <li className="flex items-center gap-1.5">
        <span className="h-2 w-4 rounded-xs border border-dashed border-fg-subtle" aria-hidden="true" />
        {t("legendWait")}
      </li>
      <li className="flex items-center gap-1.5">
        <span className="size-2 rotate-45 bg-brand" aria-hidden="true" />
        {t("legendHandoff")}
      </li>
      <li className="flex items-center gap-1.5">
        <span className="size-2 rotate-45 border-2 border-brand bg-card" aria-hidden="true" />
        {t("legendReturn")}
      </li>
      {steps ? (
        <li className="flex items-center gap-1.5">
          <span className="h-3 w-0.5 bg-foreground" aria-hidden="true" />
          {t("legendStep")}
        </li>
      ) : null}
    </ul>
  );
}

/**
 * The run's Agents section over its trace: one lane for the main agent, one for each sub-agent and one for the
 * background commands, on one time axis (agent-lanes.ts). The main agent's time waiting on a sub-agent is drawn apart
 * from its work (dashed), with a mark where it handed work over and where the report came back. "This step" and "Whole
 * run" set the range; a lane opens its agent (the main agent's trace, a sub-agent's profile, a command's call in the
 * trace). Shown whenever the run has a sub-agent or a background command, running or not. Sub-agents that ended fold
 * under one line once there are more than a few.
 */
export function AgentLanes({
  events,
  tasks,
  working,
  onShow,
  onOpen,
  onOpenMain,
  whole,
}: {
  events: readonly RunEvent[];
  tasks: readonly RunTask[];
  working: boolean;
  onShow: (call: string) => void;
  onOpen: (task: string) => void;
  onOpenMain: () => void;
  /** The Progress view: the lanes always span the whole run, with a tick on the main agent's lane where each step ended. */
  whole?: { steps: readonly { key: string; at: number }[] };
}) {
  const t = useTranslations("runs.detail.agents");
  const format = useFormatter();
  const ids = useId();
  const [picked, setScope] = useState<LaneScope>("step");
  const scope: LaneScope = whole ? "run" : picked;
  const [open, setOpen] = useState(true);
  const [finishedOpen, setFinishedOpen] = useState(false);
  const live = working && tasks.some((task) => task.status === "running");
  const now = useNow(working && open);
  const board = useMemo(() => buildLanes(events, tasks, working, now, scope, { steps: whole?.steps }), [events, tasks, working, now, scope, whole?.steps]);
  if (!board) return null;
  const { lanes, from, to } = board;
  const finished = lanes.filter((lane) => lane.kind === "agent" && lane.done);
  const folded = finished.length > FINISHED_SHOWN && !finishedOpen;
  const shown = lanes.filter((lane) => !(folded && lane.kind === "agent" && lane.done));
  const waitingFor = tasks.filter((task) => task.kind === "agent" && !task.background && working && task.status === "running").map((task) => task.name).join(", ") || null;
  const act = (lane: Lane) => {
    if (lane.kind === "main") onOpenMain();
    else if (lane.kind === "agent" && lane.task) onOpen(lane.task.id);
    else if (lane.call) onShow(lane.call);
  };
  const time = (at: number) => format.dateTime(at, { hour: "2-digit", minute: "2-digit" });
  return (
    <section aria-labelledby={`${ids}-title`} className="mt-3 mb-1 min-w-0 shrink-0 overflow-hidden rounded-lg border bg-card" data-testid="agent-lanes" data-live={live}>
      <div className="flex min-h-11 items-center gap-2 px-3 py-1.5">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={`${ids}-list`}
          onClick={() => setOpen((value) => !value)}
          className="group/head flex min-h-11 min-w-0 cursor-pointer items-center gap-2 rounded-xs text-left focus-visible:outline-2 focus-visible:outline-ring"
          data-testid="agent-lanes-toggle"
        >
          <ChevronDown className="size-4 shrink-0 text-fg-subtle transition-transform duration-[var(--transition-duration-fast)] group-aria-expanded/head:rotate-180" aria-hidden="true" />
          <h2 id={`${ids}-title`} className="text-sm leading-5 font-semibold text-foreground">
            {t("title")}
          </h2>
        </button>
        {whole ? (
          <span className="ml-auto shrink-0 rounded-lg bg-muted/60 px-2.5 py-1 text-xs leading-5 font-medium text-muted-foreground" data-testid="agent-lanes-whole">
            {t("wholeRun")}
          </span>
        ) : (
        <div role="group" aria-label={t("range")} className="ml-auto flex shrink-0 gap-0.5 rounded-lg bg-muted/60 p-0.5">
          <RangeButton id="agent-lanes-step" pressed={scope === "step"} onClick={() => setScope("step")}>
            {t("thisStep")}
          </RangeButton>
          <RangeButton id="agent-lanes-run" pressed={scope === "run"} onClick={() => setScope("run")}>
            {t("wholeRun")}
          </RangeButton>
        </div>
        )}
      </div>
      {open ? (
        <div id={`${ids}-list`} className="border-t">
          <ul aria-label={t("lanes")} className="max-h-[min(30vh,15rem)] divide-y overflow-y-auto overscroll-contain" data-testid="agent-lanes-list" data-scope={scope}>
            {shown.map((lane) => (
              <LaneRow key={lane.id} lane={lane} from={from} to={to} waitingFor={waitingFor} onOpen={act} />
            ))}
          </ul>
          {finished.length > FINISHED_SHOWN ? (
            <button
              type="button"
              aria-expanded={finishedOpen}
              onClick={() => setFinishedOpen((value) => !value)}
              className="flex min-h-11 w-full cursor-pointer items-center gap-2 border-t px-3 text-left text-xs leading-5 text-brand hover:bg-accent focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
              data-testid="agent-lanes-finished"
            >
              {finishedOpen ? t("hideFinished") : t("finished", { count: finished.length })}
            </button>
          ) : null}
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-3 py-1.5 text-xs leading-4 text-fg-subtle tabular-nums" data-testid="agent-lanes-axis">
            <time dateTime={new Date(from).toISOString()}>{time(from)}</time>
            <Legend steps={Boolean(whole)} />
            <time dateTime={new Date(to).toISOString()}>{working ? t("now") : time(to)}</time>
          </div>
        </div>
      ) : null}
    </section>
  );
}
