"use client";

import {
  ArrowDown,
  ArrowUp,
  Ban,
  BookText,
  Bug,
  CalendarClock,
  ChevronsUp,
  CircleCheckBig,
  CircleCheck,
  CircleDashed,
  CircleDot,
  CircleHelp,
  Equal,
  Eye,
  ListChecks,
  type LucideIcon,
  Play,
  Shapes,
  SquareCheckBig,
} from "lucide-react";
import type { Route } from "next";
import { useFormatter, useTranslations } from "next-intl";

import { Identifier, Tag } from "@/components/data/identifier";
import { type StatusLook, StatusPill, type StatusPillProps } from "@/components/status/status-badge";
import { cn, initials } from "@/lib/utils";

import type { Priority, SprintState, TaskGroup } from "./api";

/**
 * The Tasks pages' pills and tags. Only these pages show a task's status, priority or type, so their looks live here
 * rather than in `STATUS_LOOKS` (web/DESIGN.md, status-badge.tsx), with their words under `tasks` in both message files.
 * A status is drawn by its group, whatever the project names it: to do (neutral), doing (cobalt, as a plan step at
 * work), in review (violet), done (green), cancelled (outline); a status the config no longer lists is a question mark.
 */
export const GROUP_LOOKS = {
  todo: { tone: "neutral", icon: CircleDashed },
  doing: { tone: "running", icon: CircleDot },
  review: { tone: "review", icon: Eye },
  done: { tone: "success", icon: CircleCheck },
  cancelled: { tone: "outline", icon: Ban },
} as const satisfies Record<TaskGroup, StatusLook>;

const UNKNOWN_LOOK: StatusLook = { tone: "outline", icon: CircleHelp };

export function groupLook(group: TaskGroup | null): StatusLook {
  return group ? GROUP_LOOKS[group] : UNKNOWN_LOOK;
}

/** A task's status as a pill: the project's name for it, drawn by its group. */
export function TaskStatusBadge({ name, group, ...props }: StatusPillProps & { name: string; group: TaskGroup | null }) {
  return <StatusPill look={groupLook(group)} label={name} data-group={group ?? "other"} data-testid="task-status" {...props} />;
}

/** A sprint's state: active (green, the one at work), planned, closed. */
export const SPRINT_LOOKS = {
  active: { tone: "success", icon: Play },
  planned: { tone: "neutral", icon: CalendarClock },
  closed: { tone: "outline", icon: CircleCheckBig },
} as const satisfies Record<SprintState, StatusLook>;

export function SprintBadge({ state, ...props }: StatusPillProps & { state: SprintState }) {
  const t = useTranslations("tasks.sprintState");
  return <StatusPill look={SPRINT_LOOKS[state]} label={t(state)} data-status={state} data-testid="sprint-state" {...props} />;
}

const PRIORITY_ICONS: Record<Priority, LucideIcon> = { urgent: ChevronsUp, high: ArrowUp, medium: Equal, low: ArrowDown };

/** A task's priority as a tag; urgent in `attention`, since it asks for a person now. */
export function PriorityTag({ priority }: { priority: Priority }) {
  const t = useTranslations("tasks.priority");
  const Icon = PRIORITY_ICONS[priority];
  return (
    <Tag data-priority={priority} className={cn(priority === "urgent" && "bg-attention-soft text-attention")}>
      <Icon aria-hidden="true" />
      {t(priority)}
    </Tag>
  );
}

const TYPE_ICONS: Record<string, LucideIcon> = { task: SquareCheckBig, bug: Bug, story: BookText };

/** A task's type as a tag: the project names its types, so the word is the type itself. */
export function TypeTag({ type }: { type: string }) {
  const Icon = TYPE_ICONS[type] ?? Shapes;
  return (
    <Tag data-type={type}>
      <Icon aria-hidden="true" />
      {type}
    </Tag>
  );
}

/** A task's ref (`T-42`) in a mono chip, linking to its page when given one. */
export function TaskRef({ refName, href, testId }: { refName: string; href?: Route; testId?: string }) {
  return <Identifier value={refName} href={href} testId={testId} data-task={refName} />;
}

/** A person's initials on a neutral round, beside their login or in a stack of a task's people. Decorative. */
export function PersonAvatar({ login, className }: { login: string; className?: string }) {
  return (
    <span
      aria-hidden="true"
      title={login}
      className={cn("grid size-6 shrink-0 place-items-center rounded-full border bg-muted text-[10px] font-medium text-muted-foreground select-none", className)}
    >
      {initials(login)}
    </span>
  );
}

/**
 * A task's assignees as a stack of faces and their logins in words; "Unassigned" when there is none. `compact` (a
 * card's head) keeps the words for screen readers only, and says nothing when there is no one.
 */
export function Assignees({ logins, compact = false }: { logins: readonly string[]; compact?: boolean }) {
  const t = useTranslations("tasks.people");
  if (logins.length === 0) return compact ? null : <span className="text-xs text-fg-subtle">{t("unassigned")}</span>;
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5" data-testid="task-assignees">
      <span className="flex shrink-0 -space-x-1.5">
        {logins.slice(0, 3).map((login) => (
          <PersonAvatar key={login} login={login} className="ring-2 ring-card" />
        ))}
      </span>
      <span className={cn("min-w-0 truncate text-xs text-muted-foreground", compact && "sr-only")}>{logins.join(", ")}</span>
    </span>
  );
}

/** The codes a task links to as chips, each opening its SRS item when `hrefOf` knows where; `+2` past `max`. */
export function CodeChips({ codes, max = codes.length, hrefOf }: { codes: readonly string[]; max?: number; hrefOf?: (code: string) => Route | undefined }) {
  const t = useTranslations("tasks.card");
  if (codes.length === 0) return null;
  const shown = codes.slice(0, max);
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-1" data-testid="task-codes">
      {shown.map((code) => (
        <Identifier key={code} value={code} href={hrefOf?.(code)} data-code={code} />
      ))}
      {codes.length > shown.length ? (
        <span className="text-xs text-fg-subtle" title={codes.slice(max).join(", ")}>
          {t("more", { count: codes.length - shown.length })}
        </span>
      ) : null}
    </span>
  );
}

/** How many of the plan steps a task links are done: `1/2` with a check list, said in words for screen readers. */
export function StepsDone({ done, total }: { done: number; total: number }) {
  const t = useTranslations("tasks.card");
  if (total === 0) return null;
  return (
    <span
      className={cn("inline-flex items-center gap-1 text-xs tabular-nums", done === total ? "text-success" : "text-muted-foreground")}
      title={t("stepsDone", { done, total })}
      data-testid="task-steps"
    >
      <ListChecks className="size-3.5" aria-hidden="true" />
      <span aria-hidden="true">
        {done}/{total}
      </span>
      <span className="sr-only">{t("stepsDone", { done, total })}</span>
    </span>
  );
}

/** A sprint's days in words: "Oct 13 to Oct 26", one end alone, or nothing when it has neither. */
export function useSprintDays(): (sprint: { starts_on: string | null; ends_on: string | null }) => string | null {
  const t = useTranslations("tasks.sprint");
  const format = useFormatter();
  const day = (value: string) => format.dateTime(new Date(`${value}T00:00:00Z`), { month: "short", day: "numeric", timeZone: "UTC" });
  return ({ starts_on, ends_on }) => {
    if (starts_on && ends_on) return t("days", { from: day(starts_on), to: day(ends_on) });
    if (starts_on) return t("starts", { day: day(starts_on) });
    if (ends_on) return t("ends", { day: day(ends_on) });
    return null;
  };
}
