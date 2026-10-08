"use client";

import {
  Activity,
  Ban,
  CalendarClock,
  CircleCheck,
  CircleDashed,
  CircleDot,
  CircleHelp,
  CircleMinus,
  CircleOff,
  CirclePause,
  CirclePlay,
  CircleX,
  Clock,
  CopyX,
  Eye,
  GitMerge,
  Hand,
  Hourglass,
  LoaderCircle,
  Lock,
  type LucideIcon,
  MessageSquare,
  Moon,
  MoonStar,
  OctagonPause,
  Terminal,
  ThumbsDown,
  TimerOff,
  Unplug,
  WifiOff,
  Workflow,
} from "lucide-react";
import { useTranslations } from "next-intl";
import type { ComponentProps, ReactNode } from "react";

import type { components } from "@/lib/api/schema";
import type { RepoStatus } from "@/lib/plans";
import { cn } from "@/lib/utils";

/**
 * Every state the hub web shows as a pill: one map per kind of object, each state with its tone, its Lucide icon and
 * its words (`status.<kind>.<state>` in messages/en.json and vi.json). A fully round shape always means a state; names
 * are `Identifier` chips with square corners (web/DESIGN.md). The run, worker, plan, step and decision states come from
 * the API's own types, so a state the API adds fails typecheck until it has a look here and words in both languages.
 */

type Schemas = components["schemas"];

export type RunStatus = Schemas["Run"]["state"];
/** The hub says online, offline, draining or revoked; the web splits online into busy (it holds a run) and idle. */
export type WorkerStatus = Exclude<Schemas["Worker"]["status"], "online"> | "idle" | "busy";
/**
 * A plan's state on the web: its area (completed, or active, which the web shows only while a plan run holds the
 * plan), else blocked when its only open steps are blocked, else pending.
 */
export type PlanStatus = Schemas["PlanSummary"]["area"] | "pending" | "blocked";
/** A step's status as a plan run's worker reports it, and blocked, which only people write in a plan. */
export type StepStatus = Schemas["StepReport"]["status"] | "blocked";
export type DecisionStatus = Schemas["Decision"]["state"];
export type ProposalStatus = Schemas["ProposalSummary"]["state"];
/** Where a project's night shift stands (`CuratorStatus["state"]`), and off while it has no charter. */
export type CuratorStatusWord = NonNullable<Schemas["CuratorStatus"]["state"]> | "off";
export type { RepoStatus };

type StatusOfKind = {
  run: RunStatus;
  worker: WorkerStatus;
  plan: PlanStatus;
  step: StepStatus;
  repo: RepoStatus;
  decision: DecisionStatus;
  proposal: ProposalStatus;
  curator: CuratorStatusWord;
};
export type StatusKind = keyof StatusOfKind;
export type StatusOf<K extends StatusKind> = StatusOfKind[K];

/** The kit's tones (web/DESIGN.md, Colour): what a state means, never which page it is on. */
export type StatusTone = "neutral" | "running" | "attention" | "review" | "success" | "danger" | "outline";

export type StatusLook = {
  tone: StatusTone;
  /** The state's icon, before its word in the pill and alone where a pill would crowd (facets, step links). */
  icon: LucideIcon;
  /** A dot in the pill instead of the icon: `live` pulses (running, busy, plan run active), `static` does not. */
  dot?: "live" | "static";
};

const RUN = {
  queued: { tone: "neutral", icon: Clock },
  leased: { tone: "running", icon: Hand, dot: "static" },
  running: { tone: "running", icon: CirclePlay, dot: "live" },
  interactive: { tone: "running", icon: Terminal },
  verifying: { tone: "review", icon: LoaderCircle },
  waiting: { tone: "attention", icon: MessageSquare },
  review: { tone: "review", icon: Eye },
  parked: { tone: "attention", icon: OctagonPause },
  done: { tone: "success", icon: CircleCheck },
  failed: { tone: "danger", icon: CircleX },
  lost: { tone: "neutral", icon: Unplug },
  cancelled: { tone: "outline", icon: Ban },
} as const satisfies Record<RunStatus, StatusLook>;

const WORKER = {
  idle: { tone: "success", icon: CircleCheck },
  busy: { tone: "running", icon: Activity, dot: "live" },
  draining: { tone: "attention", icon: Hourglass },
  offline: { tone: "danger", icon: WifiOff },
  revoked: { tone: "outline", icon: Ban },
} as const satisfies Record<WorkerStatus, StatusLook>;

const PLAN = {
  pending: { tone: "neutral", icon: CircleDashed },
  active: { tone: "running", icon: Workflow, dot: "live" },
  blocked: { tone: "attention", icon: Lock },
  completed: { tone: "success", icon: CircleCheck },
} as const satisfies Record<PlanStatus, StatusLook>;

const STEP = {
  pending: { tone: "neutral", icon: CircleDashed },
  in_progress: { tone: "running", icon: CircleDot },
  blocked: { tone: "attention", icon: Lock },
  done: { tone: "success", icon: CircleCheck },
} as const satisfies Record<StepStatus, StatusLook>;

const REPO = {
  merged: { tone: "success", icon: GitMerge },
  done: { tone: "success", icon: CircleCheck },
  in_progress: { tone: "running", icon: CircleDot },
  pending: { tone: "neutral", icon: CircleDashed },
  "not-needed": { tone: "outline", icon: CircleMinus },
} as const satisfies Record<RepoStatus, StatusLook>;

const DECISION = {
  open: { tone: "attention", icon: MessageSquare },
  answered: { tone: "success", icon: CircleCheck },
  expired: { tone: "neutral", icon: TimerOff },
  cancelled: { tone: "outline", icon: Ban },
} as const satisfies Record<DecisionStatus, StatusLook>;

/** A proposal of the Curator: open waits for an admin's answer, as a decision waits for its owner's. */
const PROPOSAL = {
  open: { tone: "attention", icon: MessageSquare },
  deferred: { tone: "neutral", icon: CalendarClock },
  accepted: { tone: "success", icon: CircleCheck },
  rejected: { tone: "outline", icon: ThumbsDown },
  dropped: { tone: "outline", icon: CopyX },
} as const satisfies Record<ProposalStatus, StatusLook>;

/**
 * A project's night shift: running while a run of it is in flight (the run itself carries the pulsing dot, so this
 * pill does not); paused waits for a person to resume it.
 */
const CURATOR = {
  running: { tone: "running", icon: CirclePlay },
  on_duty: { tone: "neutral", icon: MoonStar },
  idle: { tone: "outline", icon: Moon },
  paused: { tone: "attention", icon: CirclePause },
  off: { tone: "outline", icon: CircleOff },
} as const satisfies Record<CuratorStatusWord, StatusLook>;

export const STATUS_LOOKS: { [K in StatusKind]: Record<StatusOf<K>, StatusLook> } = {
  run: RUN,
  worker: WORKER,
  plan: PLAN,
  step: STEP,
  repo: REPO,
  decision: DECISION,
  proposal: PROPOSAL,
  curator: CURATOR,
};

/** A status the hub does not know yet (a plan written by hand): neutral, with a question mark and the text as written. */
export const OTHER_LOOK: StatusLook = { tone: "outline", icon: CircleHelp };

export function statusLook<K extends StatusKind>(kind: K, status: StatusOf<K>): StatusLook {
  return STATUS_LOOKS[kind][status];
}

/** Text and ground of each tone's pill: the text token on its soft ground (web/DESIGN.md, States). */
const PILL_TONE: Record<StatusTone, string> = {
  neutral: "bg-muted text-muted-foreground",
  running: "bg-running-soft text-running",
  attention: "bg-attention-soft text-attention",
  review: "bg-review-soft text-review",
  success: "bg-success-soft text-success",
  danger: "bg-danger-soft text-danger",
  outline: "bg-transparent text-muted-foreground inset-ring inset-ring-border-strong",
};

/** The text token of each tone, for an icon shown without its pill. */
export const TONE_TEXT: Record<StatusTone, string> = {
  neutral: "text-muted-foreground",
  running: "text-running",
  attention: "text-attention",
  review: "text-review",
  success: "text-success",
  danger: "text-danger",
  outline: "text-muted-foreground",
};

/** The default test id of each kind's pill, as the e2e specs find them. */
const TEST_ID: Record<StatusKind, string> = {
  run: "run-state",
  worker: "worker-status",
  plan: "plan-state",
  step: "step-status",
  repo: "repo-status",
  decision: "decision-state",
  proposal: "proposal-state",
  curator: "curator-state",
};

type StatusKey = { [K in StatusKind]: `${K}.${StatusOf<K>}` }[StatusKind];

/** The words of a kind's states in the visitor's language: `useStatusText("run")("waiting")`. */
export function useStatusText<K extends StatusKind>(kind: K): (status: StatusOf<K>) => string {
  const t = useTranslations("status");
  // A state without words in messages/vi.json fails typecheck here; messages.test.ts keeps en.json in step.
  return (status) => t(`${kind}.${status}` as StatusKey);
}

function Mark({ look }: { look: StatusLook }) {
  if (look.dot) {
    return (
      <span
        className="relative mx-[3px] inline-flex size-2 shrink-0 rounded-full bg-current"
        aria-hidden="true"
        data-slot="status-mark"
        data-live={look.dot === "live" ? "true" : undefined}
      >
        {look.dot === "live" ? <span className="absolute inset-0 animate-live-ping rounded-full bg-current" /> : null}
      </span>
    );
  }
  return <look.icon className="size-3.5 shrink-0" aria-hidden="true" data-slot="status-mark" />;
}

type PillProps = Omit<ComponentProps<"span">, "children"> & {
  /** `lg` beside a page's h1; the default in tables, lists and cards. */
  size?: "default" | "lg";
};

function Pill({ look, label, size = "default", className, ...props }: PillProps & { look: StatusLook; label: ReactNode }) {
  return (
    <span
      data-slot="status-badge"
      data-tone={look.tone}
      className={cn(
        "inline-flex w-fit max-w-full shrink-0 items-center gap-1.5 rounded-full leading-none font-medium whitespace-nowrap",
        size === "lg" ? "h-[26px] pr-[11px] pl-[9px] text-[13px]" : "h-[22px] pr-[9px] pl-[7px] text-xs",
        PILL_TONE[look.tone],
        className,
      )}
      {...props}
    >
      <Mark look={look} />
      <span className="min-w-0 truncate">{label}</span>
    </span>
  );
}

type StatusBadgeProps = { [K in StatusKind]: { kind: K; status: StatusOf<K> } }[StatusKind] &
  PillProps & {
    /** Other words for the same state, such as a plan run's "Waiting for your decision" in its banner. */
    label?: ReactNode;
  };

/**
 * A state as a pill: its icon (or dot) and its word, in its tone. The word is the label and the icon is hidden from
 * assistive technology, so colour is never the only cue. One pill per object.
 */
export function StatusBadge({ kind, status, label, ...props }: StatusBadgeProps) {
  const text = useStatusText(kind);
  const look = STATUS_LOOKS[kind][status as never] as StatusLook;
  return (
    <Pill
      look={look}
      label={label ?? text(status as never)}
      data-kind={kind}
      data-status={status}
      data-testid={TEST_ID[kind]}
      {...props}
    />
  );
}

/** A status the hub does not know, as written in the plan: "Other status: skipped". */
export function OtherStatusBadge({ raw, ...props }: PillProps & { raw: string | null }) {
  const t = useTranslations("status");
  return <Pill look={OTHER_LOOK} label={t("other", { status: raw ?? "?" })} data-status="other" {...props} />;
}

/** A look's icon alone, in its tone, with `label` for screen readers and as a tooltip. */
export function LookIcon({ look, label, className, ...props }: { look: StatusLook; label: string } & Omit<ComponentProps<"span">, "children">) {
  return (
    <span className={cn("inline-flex shrink-0", TONE_TEXT[look.tone], className)} title={label} {...props}>
      <look.icon className="size-4" aria-hidden="true" />
      <span className="sr-only">{label}</span>
    </span>
  );
}

/** A state's icon alone, in its tone, with its word for screen readers and as a tooltip: for dense lists. */
export function StatusIcon<K extends StatusKind>({ kind, status, className }: { kind: K; status: StatusOf<K>; className?: string }) {
  const text = useStatusText(kind);
  return <LookIcon look={STATUS_LOOKS[kind][status]} label={text(status)} className={className} data-status={status} />;
}
