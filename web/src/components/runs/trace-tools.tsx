"use client";

import {
  ChevronRight,
  FilePen,
  FlaskConical,
  GitBranch,
  GitPullRequest,
  ListChecks,
  LoaderCircle,
  type LucideIcon,
  Search,
  Send,
  Terminal,
  Wrench,
} from "lucide-react";
import { useTranslations } from "next-intl";

import { useNow } from "@/hooks/use-now";
import { cn } from "@/lib/utils";

import { splitLinks } from "./forge";
import { commandGist, worktreePath } from "./shell-command";
import { callFailed, callRunning, groupDiff, groupObjects, groupPrograms, groupSpan, type Intent, programOf, type ToolGroup } from "./trace-groups";
import { type ToolCall, toolLabel } from "./trace-model";
import { KIND_ICON, useTraceDuration } from "./trace-look";
import { useTimes } from "./trace-row";
import { TermBlock } from "./trace-term";

/**
 * The trace's tool calls (agent-trace.tsx): a group of calls that did the same kind of work as one row (trace-groups.ts),
 * named by what it did, its files or programs in mono under it, its time on the right; opened, a line per call, each
 * folded to its command or path and opening on its output. A failed call shows under its group's row, open.
 */

/** Each kind of work's icon, Lucide at 16 px. */
export const INTENT_ICON: Record<Intent, LucideIcon> = {
  explore: Search,
  edit: FilePen,
  test: FlaskConical,
  check: ListChecks,
  git: GitBranch,
  github: GitPullRequest,
  hub: Send,
  run: Terminal,
  tool: Wrench,
};

function ToolMeta({ tool, active, now }: { tool: ToolCall; active: boolean; now: number | null }) {
  const t = useTranslations("runs.detail.trace");
  const short = useTraceDuration();
  const running = tool.status === "pending" || tool.status === "in_progress";
  return (
    <span className="ml-auto flex shrink-0 items-center gap-2 font-mono text-xs leading-4 whitespace-nowrap text-fg-subtle tabular-nums">
      {tool.diff ? (
        <span data-testid="trace-diffstat">
          <span className="text-success">+{tool.diff.added}</span> <span className="text-danger">−{tool.diff.removed}</span>
          <span className="sr-only">{t("diff", { added: tool.diff.added, removed: tool.diff.removed })}</span>
        </span>
      ) : null}
      {tool.exitCode !== null ? (
        <span className={tool.exitCode === 0 ? "text-success" : "text-danger"} data-testid="trace-exit">
          {t("exit", { code: tool.exitCode })}
        </span>
      ) : tool.status === "failed" ? (
        <span className="text-danger">{t("status.failed")}</span>
      ) : null}
      {running ? (
        active ? (
          <span className="inline-flex items-center gap-1 text-running" data-testid="trace-tool-running">
            <LoaderCircle className="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
            {now !== null ? short(Math.max(0, now - Date.parse(tool.startedAt))) : t("status.in_progress")}
          </span>
        ) : (
          <span>{t("noResult")}</span>
        )
      ) : tool.ms !== null ? (
        <span data-testid="trace-duration">{short(tool.ms)}</span>
      ) : null}
    </span>
  );
}

function toolState(tool: ToolCall) {
  return { running: callRunning(tool), failed: callFailed(tool) };
}

/**
 * A tool call folded to one line, the `details` that opens on its output: the line on its own row of the rail, or one
 * line of a block of calls in a row (`grouped`), which then shows the call's kind as its icon and its time in a tooltip.
 */
function ToolDetails({
  tool,
  open,
  onToggle,
  active,
  grouped = null,
}: {
  tool: ToolCall;
  open: boolean;
  onToggle: (open: boolean) => void;
  active: boolean;
  grouped?: { at: string; icon: LucideIcon | null } | null;
}) {
  const t = useTranslations("runs.detail.trace");
  const times = useTimes();
  const { running, failed } = toolState(tool);
  const now = useNow(running && active);
  const { arg } = toolLabel(tool);
  const line = arg === null ? null : tool.kind === "execute" ? commandGist(arg) : worktreePath(arg);
  // The folded line shows the argument as text; an address in it is a link in the Input once the row is open.
  const input = tool.input ?? (arg && splitLinks(arg).some((piece) => piece.href) ? arg : null);
  const nothing = !tool.output && !tool.change && !input;
  const KindIcon = grouped?.icon ?? null;
  return (
    <details
      open={open}
      onToggle={(event) => {
        const now = event.currentTarget.open;
        if (now !== open) onToggle(now);
      }}
      className={cn("group/tool min-w-0 overflow-hidden", grouped ? "bg-card" : "rounded-sm border bg-card")}
      data-testid="trace-tool"
      data-status={tool.status}
      data-kind={tool.kind}
      data-failed={failed || undefined}
    >
      <summary
        className={cn(
          "flex h-9 cursor-pointer list-none items-center gap-2 px-3 text-[13px] select-none group-open/tool:border-b hover:bg-accent max-md:h-auto max-md:min-h-11 max-md:py-2 [&::-webkit-details-marker]:hidden",
          failed && grouped && "bg-danger-soft/40",
        )}
        title={grouped ? times(grouped.at).full : undefined}
      >
        <ChevronRight
          className="size-3.5 shrink-0 text-fg-subtle transition-transform duration-[var(--transition-duration-base)] group-open/tool:rotate-90"
          aria-hidden="true"
        />
        {KindIcon ? <KindIcon className={cn("size-3.5 shrink-0", failed ? "text-danger" : "text-fg-subtle")} aria-hidden="true" /> : null}
        {grouped ? <span className="sr-only">{toolLabel(tool).name ?? t(`kind.${tool.kind}`)}: </span> : null}
        <span
          className="min-w-0 flex-1 truncate font-mono text-xs leading-4 text-muted-foreground max-md:line-clamp-2 max-md:whitespace-normal max-md:[overflow-wrap:anywhere]"
          title={arg ?? undefined}
          data-testid="trace-tool-arg"
        >
          {line ?? (tool.id ? <span className="text-fg-subtle">{tool.id}</span> : null)}
        </span>
        <span className="sr-only">, {t(`status.${tool.status}`)}</span>
        <ToolMeta tool={tool} active={active} now={now} />
      </summary>
      {open ? (
        nothing ? (
          <p className="px-3 py-2 text-xs text-fg-subtle">{running && active ? t("noOutputYet") : t("noOutput")}</p>
        ) : (
          <div className="min-w-0">
            {tool.change ? <TermBlock text={tool.change} diff testId="trace-tool-change" /> : null}
            {tool.output ? <TermBlock text={tool.output} diff={tool.kind === "edit"} testId="trace-tool-output" /> : null}
            {input ? <TermBlock text={input} label={t("input")} testId="trace-tool-input" /> : null}
          </div>
        )
      ) : null}
    </details>
  );
}

const MAX_OBJECTS = 3;

/** "a.ts, b.ts, c.ts +4": the first few, then how many more. */
function someOf(names: readonly string[]): string {
  const shown = names.slice(0, MAX_OBJECTS).join(", ");
  return names.length > MAX_OBJECTS ? `${shown} +${names.length - MAX_OBJECTS}` : shown;
}

/** A group's row: what it did ("Explored 9 files") and the mono line under it (its files, programs or last command). */
export function useGroupLabel() {
  const t = useTranslations("runs.detail.trace.group");
  return (group: ToolGroup): { title: string; detail: string | null } => {
    const count = group.calls.length;
    const last = group.calls[count - 1].tool;
    const lastLine = last.arg ? commandGist(last.arg) : null;
    const objects = groupObjects(group);
    switch (group.intent) {
      case "explore":
        return objects.length > 0
          ? { title: t("explored", { count: objects.length }), detail: someOf(objects) }
          : { title: t("exploredCode"), detail: lastLine };
      case "edit":
        return { title: t("edited", { count: Math.max(objects.length, 1) }), detail: objects.length ? someOf(objects) : lastLine };
      case "test":
        return { title: t("tests", { count }), detail: lastLine ? programOf(lastLine) : null };
      case "check":
        return { title: t("checks", { count }), detail: someOf(groupPrograms(group)) };
      case "git":
        return { title: t("git", { count }), detail: someOf(groupPrograms(group, 2).map((program) => program.replace(/^git\s+/, ""))) };
      case "github":
        return { title: t("github", { count }), detail: someOf(groupPrograms(group, 3).map((program) => program.replace(/^gh\s+/, ""))) };
      case "hub":
        return { title: t("hub", { count }), detail: lastLine ? programOf(lastLine) : null };
      case "run":
        return { title: t("commands", { count }), detail: lastLine ? programOf(lastLine) : null };
      case "tool":
        return { title: t("tools", { names: someOf([...new Set(group.calls.map(({ tool }) => toolLabel(tool).name ?? t("tool")))]) }), detail: lastLine };
    }
  };
}

/**
 * One group of tool calls as a row: its kind's icon (`running` while its last call runs, `danger` when one failed),
 * what it did, how many failed, its files or programs in mono, and its time from the first call's start to the last
 * call's end, a clock while it runs. A button opens it on a line per call; its failed calls show under it either way.
 * `open` holds the group's state and the keys of its calls that are open: "1:a,b".
 */
export function GroupRow({ group, open, onToggle, active }: { group: ToolGroup; open: string; onToggle: (key: string, open: boolean) => void; active: boolean }) {
  const t = useTranslations("runs.detail.trace.group");
  const label = useGroupLabel();
  const short = useTraceDuration();
  const [groupOpen, keys] = open.split(":");
  const expanded = groupOpen === "1";
  const openCalls = new Set(keys ? keys.split(",") : []);
  const last = group.calls[group.calls.length - 1].tool;
  const running = active && callRunning(last);
  const now = useNow(running);
  const failed = group.calls.filter(({ tool }) => callFailed(tool));
  const diff = group.intent === "edit" ? groupDiff(group) : null;
  const span = groupSpan(group);
  const { title, detail } = label(group);
  const Icon = INTENT_ICON[group.intent];
  const shown = expanded ? group.calls : failed;
  return (
    <div className="flex min-w-0 flex-col" data-testid="trace-group" data-intent={group.intent} data-count={group.calls.length} data-running={running || undefined}>
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => onToggle(group.key, !expanded)}
        className="group/row -mx-2 flex min-w-0 cursor-pointer items-start gap-2.5 rounded-md px-2 py-2 text-left transition-colors duration-[var(--transition-duration-fast)] hover:bg-accent focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring max-md:min-h-11"
        data-testid="trace-group-toggle"
      >
        <Icon className={cn("mt-0.5 size-4 shrink-0", running ? "text-running" : failed.length ? "text-danger" : "text-muted-foreground")} aria-hidden="true" />
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="text-sm leading-5 font-medium text-foreground max-md:text-[15px] max-md:leading-[22px]" data-testid="trace-group-title">
            {title}
            {failed.length ? <span className="font-normal text-danger">{`, ${t("failed", { count: failed.length })}`}</span> : null}
            {diff ? (
              <span className="ml-2 font-mono text-xs font-normal tabular-nums">
                <span className="text-success">+{diff.added}</span> <span className="text-danger">−{diff.removed}</span>
              </span>
            ) : null}
          </span>
          {detail ? (
            <span className="truncate font-mono text-xs leading-4 text-fg-subtle" title={detail} data-testid="trace-group-detail">
              {detail}
            </span>
          ) : null}
        </span>
        <span className={cn("mt-0.5 shrink-0 font-mono text-xs leading-4 tabular-nums", running ? "text-running" : "text-fg-subtle")}>
          {running ? (now !== null ? short(Math.max(0, now - Date.parse(group.calls[0].tool.startedAt))) : null) : span !== null ? short(span) : null}
        </span>
        <ChevronRight
          className="mt-0.5 size-4 shrink-0 text-fg-subtle transition-transform duration-[var(--transition-duration-fast)] group-aria-expanded/row:rotate-90"
          aria-hidden="true"
        />
      </button>
      {shown.length ? (
        <ol className="mt-1 mb-1 ml-[26px] min-w-0 divide-y overflow-hidden rounded-md border" data-testid="trace-tools">
          {shown.map((entry) => (
            <li key={entry.key} data-seq={entry.seq}>
              <ToolDetails
                tool={entry.tool}
                open={openCalls.has(entry.key)}
                onToggle={(value) => onToggle(entry.key, value)}
                active={active}
                grouped={{ at: entry.at, icon: group.intent === "tool" ? KIND_ICON[entry.tool.kind] : null }}
              />
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}
