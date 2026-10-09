"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import {
  ArrowDown,
  Bot,
  Braces,
  Brain,
  ChevronRight,
  Circle,
  CircleCheck,
  CircleDot,
  CircleX,
  Info,
  ListChecks,
  LoaderCircle,
  type LucideIcon,
  MessageSquare,
  User,
} from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { memo, type ReactNode, type RefObject, useDeferredValue, useLayoutEffect, useMemo, useRef, useState } from "react";

import { useNow } from "@/components/kg/use-now";
import { SafeMarkdown } from "@/components/memories/markdown";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import type { DescribeMove } from "./log-model";
import { decisionAnchor, decisionHref, type RunEvent } from "./queries";
import {
  buildTrace,
  cutOutput,
  openByDefault,
  type ToolCall,
  toolLabel,
  TRACE_VIRTUAL_THRESHOLD,
  type TraceItem,
} from "./trace-model";
import { KIND_ICON, useTraceDuration } from "./trace-look";
import type { LogStatus } from "./use-run-log";

/**
 * The kit's AgentTrace, the default tab of a run's page: what the agent did, step by step (trace-model.ts reads the
 * events): its messages, its thinking folded under "Thought for 9s", each tool call as one row (folded, opened when it
 * failed; the output on `term-bg`, cut at 20 lines with "Show the full output"), the plan as a checklist, the owner's
 * messages, "Asked you" for a decision with a link to its card, and the moves between states in `caption`. A running
 * run ends with the typing dots. It reads the same events as the Raw log (SSE, then polling), in a `role="log"` region
 * (polite) that follows the newest item unless the person scrolled up; past 500 items only the ones in view render.
 * Tool rows are `details`, so they open and close without script.
 */

/** How close to the end (px) still counts as at the end. */
const END_SLACK = 32;
/** An item's height before it is measured. */
const ITEM_ESTIMATE = 64;

export type TraceContext = {
  runId: number;
  /** The login of the run's owner, whom the agent asks. */
  owner: string;
  /** The visitor's login, to say "You". */
  viewer: string | null;
  describe: DescribeMove;
  /** Decisions open now whose card is on this page, which "Asked you" links to; the others link to the Inbox. */
  openDecisions: ReadonlySet<number>;
  /** The agent is at work: the trace ends with the typing dots. */
  working: boolean;
  /** The run may still write events. */
  active: boolean;
};

function useTimes() {
  const format = useFormatter();
  return (at: string) => {
    const date = new Date(at);
    return { short: format.dateTime(date, { timeStyle: "medium" }), full: format.dateTime(date, { dateStyle: "full", timeStyle: "long" }) };
  };
}

function Time({ at }: { at: string }) {
  const times = useTimes();
  const { short, full } = times(at);
  return (
    <time dateTime={at} title={full} className="self-center font-mono text-xs leading-4 whitespace-nowrap text-fg-subtle tabular-nums">
      {short}
    </time>
  );
}

type Look = "agent" | "user" | "tool" | "error" | "ask" | "live" | "system";

const ICON_LOOK: Record<Look, string> = {
  agent: "text-brand",
  user: "bg-muted text-foreground",
  tool: "text-muted-foreground",
  error: "text-danger",
  ask: "border-attention-solid bg-attention-soft text-attention",
  live: "border-running text-running",
  system: "h-[22px] border-0 bg-transparent text-fg-subtle",
};

/** One event of the kit's trace: an icon on the rail, a head, the time, and a body under the head. */
function Row({ icon: Icon, look, head, at, children, system = false }: { icon: LucideIcon; look: Look; head: ReactNode; at: string | null; children?: ReactNode; system?: boolean }) {
  return (
    <div className={cn("grid grid-cols-[28px_minmax(0,1fr)_auto] gap-x-3 gap-y-2", system ? "pb-2" : "pt-2 pb-3")}>
      <span className={cn("relative z-[1] grid size-7 place-items-center rounded-sm border bg-card", ICON_LOOK[look])} aria-hidden="true">
        <Icon className={system ? "size-3.5" : "size-4"} />
      </span>
      <div className={cn("flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5", system ? "min-h-[22px] text-xs text-fg-subtle" : "min-h-7 text-[13px] text-muted-foreground")}>
        {head}
      </div>
      {at ? <Time at={at} /> : <span />}
      {children ? <div className="col-span-2 col-start-2 min-w-0">{children}</div> : null}
    </div>
  );
}

function Who({ children }: { children: ReactNode }) {
  return <span className="font-semibold text-foreground">{children}</span>;
}

/** Text on the terminal surface, cut at 20 lines unless the person asks for all of it. */
function TermBlock({ text, diff = false, label, testId }: { text: string; diff?: boolean; label?: string; testId: string }) {
  const t = useTranslations("runs.detail.trace");
  const [whole, setWhole] = useState(false);
  const { shown, total, cut } = cutOutput(text);
  const body = whole ? text.replace(/\n$/, "") : shown;
  return (
    <div className="min-w-0 border-t border-term-border first:border-t-0" data-testid={testId}>
      {label ? <p className="bg-term-bg px-3 pt-2 font-mono text-[11px] leading-4 text-term-muted">{label}</p> : null}
      <pre
        className="m-0 max-h-[32rem] overflow-auto bg-term-bg px-3 py-2.5 font-mono text-xs leading-[18px] whitespace-pre text-term-fg [font-variant-ligatures:none]"
        tabIndex={0}
      >
        {diff
          ? body.split("\n").map((line, index) => (
              <span key={index} className={cn("block", line.startsWith("+") ? "text-term-ok" : line.startsWith("-") ? "text-term-error" : "")}>
                {line || " "}
              </span>
            ))
          : body}
      </pre>
      {cut ? (
        <button
          type="button"
          className="block w-full border-t border-term-border bg-term-bg px-3 py-1.5 text-left font-mono text-xs text-term-muted hover:bg-term-row hover:text-term-fg max-md:min-h-11"
          aria-expanded={whole}
          onClick={() => setWhole((value) => !value)}
          data-testid="trace-output-more"
        >
          {whole ? t("showLess", { count: 20 }) : t("showFull", { count: total })}
        </button>
      ) : null}
    </div>
  );
}

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

function ToolRow({ tool, at, open, onToggle, active }: { tool: ToolCall; at: string; open: boolean; onToggle: (open: boolean) => void; active: boolean }) {
  const t = useTranslations("runs.detail.trace");
  const running = tool.status === "pending" || tool.status === "in_progress";
  const now = useNow(running && active);
  const failed = tool.status === "failed" || (tool.exitCode !== null && tool.exitCode !== 0);
  const { name: named, arg } = toolLabel(tool);
  const name = named ?? t(`kind.${tool.kind}`);
  const nothing = !tool.output && !tool.change && !tool.input;
  return (
    <Row
      icon={KIND_ICON[tool.kind]}
      look={failed ? "error" : running && active ? "live" : "tool"}
      at={at}
      head={
        <>
          <Who>{name}</Who>
          {tool.description ? <span className="min-w-0 truncate">{tool.description}</span> : null}
        </>
      }
    >
      <details
        open={open}
        onToggle={(event) => {
          const now = event.currentTarget.open;
          if (now !== open) onToggle(now);
        }}
        className="group/tool min-w-0 overflow-hidden rounded-sm border bg-card"
        data-testid="trace-tool"
        data-status={tool.status}
        data-kind={tool.kind}
        data-failed={failed || undefined}
      >
        <summary className="flex h-9 cursor-pointer list-none items-center gap-2 px-3 text-[13px] select-none group-open/tool:border-b hover:bg-accent max-md:h-11 [&::-webkit-details-marker]:hidden">
          <ChevronRight
            className="size-3.5 shrink-0 text-fg-subtle transition-transform duration-[var(--transition-duration-base)] group-open/tool:rotate-90"
            aria-hidden="true"
          />
          <span className="min-w-0 flex-1 truncate font-mono text-xs leading-4 text-muted-foreground" title={arg ?? undefined} data-testid="trace-tool-arg">
            {arg ?? (tool.id ? <span className="text-fg-subtle">{tool.id}</span> : null)}
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
              {tool.input ? <TermBlock text={tool.input} label={t("input")} testId="trace-tool-input" /> : null}
            </div>
          )
        ) : null}
      </details>
    </Row>
  );
}

function AgentRow({ item }: { item: Extract<TraceItem, { type: "agent" }> }) {
  const t = useTranslations("runs.detail.trace");
  const short = useTraceDuration();
  const thought = item.thought;
  return (
    <Row icon={Bot} look="agent" at={item.at} head={<Who>{t("agent")}</Who>}>
      <div className="flex min-w-0 flex-col gap-2">
        {thought ? (
          <details className="group/thought min-w-0 text-[13px] leading-[19px] text-muted-foreground" data-testid="trace-thought">
            <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 text-fg-subtle select-none hover:text-foreground max-md:min-h-11 [&::-webkit-details-marker]:hidden">
              <Brain className="size-3.5" aria-hidden="true" />
              {thought.ms >= 1000 ? t("thought", { duration: short(thought.ms) }) : t("thoughtBrief")}
              <ChevronRight className="size-3 transition-transform duration-[var(--transition-duration-base)] group-open/thought:rotate-90" aria-hidden="true" />
            </summary>
            <div className="mt-2 border-l-2 border-border pl-3 break-words whitespace-pre-wrap">{thought.text}</div>
          </details>
        ) : null}
        {item.text ? <SafeMarkdown className="leading-[21px]" testId="trace-message">{item.text}</SafeMarkdown> : null}
      </div>
    </Row>
  );
}

function PlanRow({ item }: { item: Extract<TraceItem, { type: "plan" }> }) {
  const t = useTranslations("runs.detail.trace");
  const done = item.entries.filter((entry) => entry.status === "completed").length;
  return (
    <Row
      icon={ListChecks}
      look="agent"
      at={item.at}
      head={
        <>
          <Who>{t("plan")}</Who>
          <span className="tabular-nums">{t("planCount", { done, total: item.entries.length })}</span>
        </>
      }
    >
      <ul className="flex flex-col gap-1 text-sm" data-testid="trace-plan">
        {item.entries.map((entry, index) => {
          const Icon = entry.status === "completed" ? CircleCheck : entry.status === "in_progress" ? CircleDot : Circle;
          return (
            <li key={index} className="flex min-w-0 items-start gap-2" data-status={entry.status}>
              <Icon
                className={cn(
                  "mt-0.5 size-4 shrink-0",
                  entry.status === "completed" ? "text-success" : entry.status === "in_progress" ? "text-running" : "text-fg-subtle",
                )}
                aria-hidden="true"
              />
              <span className="sr-only">{t(`planStatus.${entry.status}`)}: </span>
              <span className={cn("min-w-0 [overflow-wrap:anywhere]", entry.status === "completed" && "text-muted-foreground")}>{entry.content}</span>
            </li>
          );
        })}
      </ul>
    </Row>
  );
}

function AskRow({ item, context }: { item: Extract<TraceItem, { type: "ask" }>; context: TraceContext }) {
  const t = useTranslations("runs.detail.trace");
  const mine = context.viewer !== null && context.viewer === context.owner;
  const onPage = context.openDecisions.has(item.decisionId);
  const link = "shrink-0 font-medium text-brand underline-offset-4 hover:text-brand-hover hover:underline max-md:inline-flex max-md:min-h-11 max-md:items-center";
  return (
    <Row
      icon={MessageSquare}
      look="ask"
      at={item.at}
      head={
        <>
          <Who>{mine ? t("askedYou") : t("asked", { login: context.owner })}</Who>
          <span className="min-w-0 text-foreground [overflow-wrap:anywhere]" data-testid="trace-ask-question">
            {item.question}
          </span>
          {onPage ? (
            <a href={`#${decisionAnchor(item.decisionId)}`} className={link} data-testid="trace-ask-link">
              {mine ? t("answer") : t("viewDecision")}
            </a>
          ) : (
            <Link href={decisionHref(item.decisionId)} className={link} data-testid="trace-ask-link">
              {t("inInbox")}
            </Link>
          )}
        </>
      }
    />
  );
}

function UserRow({ item, context }: { item: Extract<TraceItem, { type: "user" }>; context: TraceContext }) {
  const t = useTranslations("runs.detail.trace");
  const you = item.from !== null && item.from === context.viewer;
  const who = you ? t("you") : (item.from ?? t("someone"));
  return (
    <Row
      icon={User}
      look="user"
      at={item.at}
      head={
        <Who>
          {item.decisionId !== null
            ? you
              ? t("youAnswered", { id: item.decisionId })
              : t("answered", { login: who, id: item.decisionId })
            : who}
        </Who>
      }
    >
      <p className="text-sm leading-[21px] break-words whitespace-pre-wrap text-foreground">{item.text}</p>
    </Row>
  );
}

function SystemRow({ item }: { item: Extract<TraceItem, { type: "system" }> }) {
  const t = useTranslations("runs.detail.trace");
  const Icon = item.tone === "ok" ? CircleCheck : item.tone === "error" ? CircleX : Info;
  return (
    <Row
      icon={Icon}
      look="system"
      system
      at={item.at}
      head={<span className={cn("min-w-0 break-words whitespace-pre-wrap", item.tone === "error" && "text-danger", item.tone === "ok" && "text-success")}>{item.text}</span>}
    >
      {item.output ? (
        <details className="group/out min-w-0 overflow-hidden rounded-sm border" data-testid="trace-system-output">
          <summary className="flex h-8 cursor-pointer list-none items-center gap-2 px-3 text-xs text-muted-foreground select-none hover:bg-accent max-md:h-11 [&::-webkit-details-marker]:hidden">
            <ChevronRight className="size-3.5 transition-transform duration-[var(--transition-duration-base)] group-open/out:rotate-90" aria-hidden="true" />
            {t("systemOutput")}
          </summary>
          <TermBlock text={item.output} testId="trace-system-output-text" />
        </details>
      ) : null}
    </Row>
  );
}

function MoveRow({ item, context }: { item: Extract<TraceItem, { type: "move" }>; context: TraceContext }) {
  const bad = item.move.to === "failed" || item.move.to === "lost";
  const good = item.move.to === "done";
  return (
    <Row
      icon={bad ? CircleX : good ? CircleCheck : CircleDot}
      look="system"
      system
      at={item.at}
      head={<span className={cn("min-w-0 [overflow-wrap:anywhere]", bad && "text-danger", good && "text-success")}>{context.describe(item.move)}</span>}
    />
  );
}

function RawRow({ item }: { item: Extract<TraceItem, { type: "raw" }> }) {
  const t = useTranslations("runs.detail.trace");
  const labels = [...new Set(item.events.map((event) => event.label))].join(", ");
  const text = item.events.map((event) => `#${event.seq} ${event.label}\n${event.json}`).join("\n\n");
  // One caption line, like a move: what the runtime sent that the trace does not read, folded with its raw JSON.
  return (
    <div className="grid grid-cols-[28px_minmax(0,1fr)] gap-x-3 pb-2">
      <span className="relative z-[1] grid h-[22px] w-7 place-items-center text-fg-subtle" aria-hidden="true">
        <Braces className="size-3.5" />
      </span>
      <details className="group/raw min-w-0" data-testid="trace-raw">
        <summary className="flex min-h-[22px] cursor-pointer list-none items-center gap-2 text-xs text-fg-subtle select-none hover:text-foreground max-md:min-h-11 [&::-webkit-details-marker]:hidden">
          <span className="shrink-0">{t("raw", { count: item.events.length })}</span>
          <ChevronRight className="size-3.5 shrink-0 transition-transform duration-[var(--transition-duration-base)] group-open/raw:rotate-90" aria-hidden="true" />
          <span className="min-w-0 flex-1 truncate font-mono">{labels}</span>
          <Time at={item.at} />
        </summary>
        <div className="mt-2 overflow-hidden rounded-sm border border-term-border">
          <TermBlock text={text} testId="trace-raw-json" />
        </div>
      </details>
    </div>
  );
}

const Item = memo(
  function Item({ item, context, open, onToggle }: { item: TraceItem; context: TraceContext; open: boolean; onToggle: (key: string, open: boolean) => void; signature: string }) {
    switch (item.type) {
      case "agent":
        return <AgentRow item={item} />;
      case "tool":
        return <ToolRow tool={item.tool} at={item.at} open={open} onToggle={(value) => onToggle(item.key, value)} active={context.active} />;
      case "plan":
        return <PlanRow item={item} />;
      case "ask":
        return <AskRow item={item} context={context} />;
      case "user":
        return <UserRow item={item} context={context} />;
      case "move":
        return <MoveRow item={item} context={context} />;
      case "system":
        return <SystemRow item={item} />;
      case "raw":
        return <RawRow item={item} />;
    }
  },
  (before, after) =>
    before.signature === after.signature && before.open === after.open && before.context === after.context && before.onToggle === after.onToggle,
);

/** What makes an item look different: the memo of a row re-renders only when it changes. */
function signature(item: TraceItem): string {
  switch (item.type) {
    case "agent":
      return `${item.key}:${item.text.length}:${item.thought?.text.length ?? -1}:${item.thought?.ms ?? -1}`;
    case "tool": {
      const tool = item.tool;
      return `${item.key}:${tool.status}:${tool.title}:${tool.arg}:${tool.ms}:${tool.exitCode}:${tool.output?.length ?? -1}:${tool.diff?.added}:${tool.diff?.removed}`;
    }
    case "raw":
      return `${item.key}:${item.events.length}`;
    default:
      return item.key;
  }
}

function Typing() {
  const t = useTranslations("runs.detail.trace");
  return (
    <div className="grid grid-cols-[28px_minmax(0,1fr)] gap-x-3 pt-2 pb-1" data-testid="trace-typing">
      <span className="grid size-7 place-items-center rounded-sm border border-running bg-card text-running" aria-hidden="true">
        <Bot className="size-4" />
      </span>
      <span className="flex items-center gap-[3px]">
        <span className="sr-only">{t("working")}</span>
        {[0, 1, 2].map((dot) => (
          <i key={dot} aria-hidden="true" className="size-[5px] animate-typing rounded-full bg-running" style={{ animationDelay: `${dot * 0.2}s` }} />
        ))}
      </span>
    </div>
  );
}

type Virtual = ReturnType<typeof useVirtualizer<HTMLDivElement, HTMLLIElement>>;

const LINE = "relative before:absolute before:top-[38px] before:-bottom-0.5 before:left-[13.5px] before:w-px before:bg-border last:before:hidden";
const SYSTEM_LINE = "before:top-[24px]";

function lineClass(item: TraceItem) {
  return cn(LINE, (item.type === "move" || item.type === "system" || item.type === "raw") && SYSTEM_LINE);
}

function VirtualItems({
  items,
  render,
  scrollRef,
  virtualRef,
}: {
  items: readonly TraceItem[];
  render: (item: TraceItem) => ReactNode;
  scrollRef: RefObject<HTMLDivElement | null>;
  virtualRef: RefObject<Virtual | null>;
}) {
  // The app does not use the React Compiler, which this rule is about; the virtualizer re-renders through its own state.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer<HTMLDivElement, HTMLLIElement>({
    count: items.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ITEM_ESTIMATE,
    getItemKey: (index) => items[index].key,
    overscan: 8,
  });
  useLayoutEffect(() => {
    virtualRef.current = virtualizer;
    return () => {
      virtualRef.current = null;
    };
  }, [virtualRef, virtualizer]);
  return (
    <ol className="relative" style={{ height: virtualizer.getTotalSize() }}>
      {virtualizer.getVirtualItems().map((row) => {
        const item = items[row.index];
        return (
          <li
            key={row.key}
            ref={virtualizer.measureElement}
            data-index={row.index}
            className={cn("absolute inset-x-0 top-0", lineClass(item), row.index === items.length - 1 && "before:hidden")}
            style={{ transform: `translateY(${row.start}px)` }}
            data-testid="trace-item"
            data-type={item.type}
            data-seq={item.seq}
          >
            {render(item)}
          </li>
        );
      })}
    </ol>
  );
}

export function AgentTrace({
  events,
  status,
  context,
  shown = true,
  fill = false,
}: {
  events: readonly RunEvent[];
  status: LogStatus;
  context: TraceContext;
  shown?: boolean;
  /**
   * From the xl breakpoint, take the height the parent gives (the run page's session card, as tall as the window
   * allows) instead of growing with the items up to 70 % of the window.
   */
  fill?: boolean;
}) {
  const t = useTranslations("runs.detail.trace");
  const tLog = useTranslations("runs.detail.log");
  const deferred = useDeferredValue(events);
  const items = useMemo(() => buildTrace(deferred), [deferred]);
  const [toggled, setToggled] = useState<ReadonlyMap<string, boolean>>(() => new Map());
  const [follow, setFollow] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const virtualRef = useRef<Virtual | null>(null);
  const lastTop = useRef(0);
  const virtual = items.length > TRACE_VIRTUAL_THRESHOLD;
  const typing = context.working && status !== "failed";

  const onToggle = useMemo(
    () => (key: string, open: boolean) =>
      setToggled((previous) => {
        const next = new Map(previous);
        next.set(key, open);
        return next;
      }),
    [],
  );
  const isOpen = (item: TraceItem) => (item.type === "tool" ? (toggled.get(item.key) ?? openByDefault(item.tool)) : false);
  const render = (item: TraceItem) => <Item item={item} context={context} open={isOpen(item)} onToggle={onToggle} signature={signature(item)} />;

  const toEnd = () => {
    const element = scrollRef.current;
    if (!element) return;
    if (virtual && virtualRef.current && items.length) virtualRef.current.scrollToIndex(items.length - 1, { align: "end" });
    element.scrollTop = element.scrollHeight;
    lastTop.current = element.scrollTop;
  };

  // Keep the newest item in view while following, and when the tab is shown again (hidden, the trace cannot scroll).
  useLayoutEffect(() => {
    if (!follow || !shown || items.length === 0) return;
    toEnd();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- toEnd reads refs and `virtual`, which `items` decides
  }, [items, follow, shown, typing]);

  // The page only ever scrolls the trace down, to its end; a move up away from the end is the person's.
  const onScroll = () => {
    const element = scrollRef.current;
    if (!element) return;
    const top = element.scrollTop;
    const atEnd = element.scrollHeight - top - element.clientHeight <= END_SLACK;
    if (follow && !atEnd && top < lastTop.current - 2) setFollow(false);
    else if (!follow && atEnd && top > lastTop.current) setFollow(true);
    lastTop.current = top;
  };

  const empty = items.length === 0;

  return (
    <div className={cn("relative min-w-0", fill && "xl:flex xl:min-h-0 xl:flex-1 xl:flex-col")}>
      <div
        ref={scrollRef}
        role="log"
        aria-live="polite"
        aria-relevant="additions"
        aria-label={t("label", { id: context.runId })}
        aria-busy={status === "connecting" || undefined}
        tabIndex={0}
        onScroll={onScroll}
        className={cn(
          // Relative, so the typing dots' words for screen readers are clipped with the items rather than reaching
          // past the trace and lengthening the page.
          "relative max-h-[min(70vh,44rem)] min-h-48 overflow-y-auto overscroll-contain px-4 pt-2 pb-3 focus-visible:outline-offset-[-2px]",
          fill && "xl:max-h-none xl:min-h-0 xl:flex-1",
        )}
        data-testid="trace"
        data-virtual={virtual || undefined}
        data-follow={follow}
      >
        {empty ? (
          <p className="py-2 text-sm text-muted-foreground" data-testid="trace-empty">
            {events.length > 0 ? t("nothing") : context.active || status === "connecting" ? tLog("waiting") : tLog("none")}
          </p>
        ) : virtual ? (
          <VirtualItems items={items} render={render} scrollRef={scrollRef} virtualRef={virtualRef} />
        ) : (
          <ol className="flex flex-col">
            {items.map((item) => (
              <li key={item.key} className={lineClass(item)} data-testid="trace-item" data-type={item.type} data-seq={item.seq}>
                {render(item)}
              </li>
            ))}
          </ol>
        )}
        {typing && !empty ? <Typing /> : null}
      </div>
      {!follow && !empty ? (
        <Button
          type="button"
          variant="secondary"
          size="sm"
          className="absolute bottom-3 left-1/2 -translate-x-1/2 shadow-popover"
          onClick={() => {
            setFollow(true);
            toEnd();
          }}
          data-testid="trace-latest"
        >
          <ArrowDown aria-hidden="true" />
          {t("latest")}
        </Button>
      ) : null}
    </div>
  );
}
