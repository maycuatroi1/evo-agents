"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import {
  ArrowDown,
  Bot,
  Brain,
  ChevronRight,
  Circle,
  CircleCheck,
  CircleDot,
  CircleX,
  Info,
  ListChecks,
  MessageSquare,
  User,
} from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { memo, type ReactNode, type RefObject, useDeferredValue, useLayoutEffect, useMemo, useRef, useState } from "react";

import { SafeMarkdown } from "@/components/memories/markdown";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import type { RepoWeb } from "./forge";
import { LinkedText } from "./links";
import type { DescribeMove } from "./log-model";
import { decisionAnchor, decisionHref, type RunEvent } from "./queries";
import { TraceTyping } from "./chat-parts";
import { holdsStepReport } from "./step-report-start";
import {
  buildTrace,
  openByDefault,
  TRACE_VIRTUAL_THRESHOLD,
  type TraceItem,
} from "./trace-model";
import { useTraceDuration } from "./trace-look";
import { StepReportMessage } from "./trace-report-lazy";
import { Row, Who } from "./trace-row";
import { TermBlock } from "./trace-term";
import { callRunning, groupByIntent, type NormalItem } from "./trace-groups";
import { GroupRow } from "./trace-tools";
import type { LogStatus } from "./use-run-log";

/**
 * The kit's AgentTrace, the default tab of a run's page: what the agent did, step by step (trace-model.ts reads the
 * events): its messages, its thinking folded under "Thought for 9s", each tool call as one row (folded, opened when it
 * failed; the output on `term-bg`, cut at 20 lines with "Show the full output"), the plan as a checklist, the owner's
 * messages, "Asked you" for a decision with a link to its card, and the moves between states in `caption`. A step's
 * report, the message a step's agent ends with, is a card (trace-report.tsx). A running run ends with the typing dots. It reads the same events as the Raw log (SSE, then polling), in a `role="log"` region
 * (polite) that follows the newest item unless the person scrolled up; past 500 items only the ones in view render.
 * Tool rows are `details`, so they open and close without script. Every http and https address in what the trace shows
 * is a link to a new tab (links.tsx); the one in a tool row's folded line is text there, since a link inside its
 * `summary` would also open and close the row, and the row's Input shows it as a link once opened. A shell call's
 * folded line is what the command does once its setup is done (`commandGist`), the whole command in its Input. Under
 * 768 px tool calls in a row are one block (trace-tools.tsx), a folded line takes two lines, times drop their seconds,
 * and the runtime events the trace does not read are left to the Raw log, with one line saying how many.
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
  /** An author run's: opens a decision's card in the Chat tab, where every decision of the run is. */
  showDecision?: (id: number) => void;
  /** The agent is at work: the trace ends with the typing dots. */
  working: boolean;
  /** The run may still write events. */
  active: boolean;
  /** The web page of the run's repo on a branch, which a step report's branch and commits link to. */
  webOfBranch?: (branch: string | null) => RepoWeb | null;
};


const reportProse = (text: string) => <SafeMarkdown className="text-[13px] leading-5" testId="trace-report-text">{text}</SafeMarkdown>;
const message = (text: string) => <SafeMarkdown className="leading-[21px]" testId="trace-message">{text}</SafeMarkdown>;

function AgentRow({ item, context }: { item: Extract<TraceItem, { type: "agent" }>; context: TraceContext }) {
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
            <div className="mt-2 border-l-2 border-border pl-3 break-words whitespace-pre-wrap">
              <LinkedText text={thought.text} />
            </div>
          </details>
        ) : null}
        {/* A step's report, which Markdown would fold into one paragraph, shows as a card between the words around it. */}
        {holdsStepReport(item.text) ? (
          <StepReportMessage text={item.text} message={message} prose={reportProse} webOf={(branch) => context.webOfBranch?.(branch) ?? null} />
        ) : item.text ? (
          message(item.text)
        ) : null}
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
              <span className={cn("min-w-0 [overflow-wrap:anywhere]", entry.status === "completed" && "text-muted-foreground")}>
                <LinkedText text={entry.content} />
              </span>
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
            <LinkedText text={item.question} />
          </span>
          {onPage || context.showDecision ? (
            <a
              href={`#${decisionAnchor(item.decisionId)}`}
              className={link}
              onClick={(event) => {
                if (!context.showDecision) return;
                event.preventDefault();
                context.showDecision(item.decisionId);
              }}
              data-testid="trace-ask-link"
            >
              {mine && onPage ? t("answer") : t("viewDecision")}
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
      <p className="text-sm leading-[21px] break-words whitespace-pre-wrap text-foreground">
        <LinkedText text={item.text} />
      </p>
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
      head={
        <span className={cn("min-w-0 break-words whitespace-pre-wrap", item.tone === "error" && "text-danger", item.tone === "ok" && "text-success")}>
          <LinkedText text={item.text} />
        </span>
      }
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
      head={
        <span className={cn("min-w-0 [overflow-wrap:anywhere]", bad && "text-danger", good && "text-success")}>
          <LinkedText text={context.describe(item.move)} />
        </span>
      }
    />
  );
}

const Item = memo(
  function Item({
    item,
    context,
    open,
    onToggle,
  }: {
    item: NormalItem;
    context: TraceContext;
    /** A group of tool calls': "1:a,b", whether it is open and the keys of its open calls. */
    open: string;
    onToggle: (key: string, open: boolean) => void;
    signature: string;
  }) {
    switch (item.type) {
      case "group":
        return <GroupRow group={item} open={open} onToggle={onToggle} active={context.active} />;
      case "agent":
        return <AgentRow item={item} context={context} />;
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
    }
  },
  (before, after) =>
    before.signature === after.signature && before.open === after.open && before.context === after.context && before.onToggle === after.onToggle,
);

/** What makes an item look different: the memo of a row re-renders only when it changes. */
function signature(item: NormalItem): string {
  switch (item.type) {
    case "agent":
      return `${item.key}:${item.text.length}:${item.thought?.text.length ?? -1}:${item.thought?.ms ?? -1}`;
    case "group":
      return item.calls
        .map(({ key, tool }) => `${key}:${tool.status}:${tool.title}:${tool.arg}:${tool.ms}:${tool.exitCode}:${tool.output?.length ?? -1}:${tool.diff?.added}:${tool.diff?.removed}`)
        .join("|");
    default:
      return item.key;
  }
}

type Virtual = ReturnType<typeof useVirtualizer<HTMLDivElement, HTMLLIElement>>;

/**
 * The rail from an item's icon down to the next one. It says nothing of the item's position, which each list sets: in
 * flow and `relative` in the plain list, `absolute` in the virtual one; a `relative` here would win over that
 * `absolute` in `cn` and put every virtual row back in flow, below the ones rendered before it.
 */
const LINE = "";

function lineClass(item: NormalItem) {
  return cn(LINE, item.type === "group" ? "py-0" : null);
}

function VirtualItems({
  items,
  render,
  scrollRef,
  virtualRef,
}: {
  items: readonly NormalItem[];
  render: (item: NormalItem) => ReactNode;
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
  fill?: boolean | "always";
}) {
  const t = useTranslations("runs.detail.trace");
  const tLog = useTranslations("runs.detail.log");
  const deferred = useDeferredValue(events);
  const built = useMemo(() => buildTrace(deferred), [deferred]);
  // Calls that did the same kind of work are one row; the runtime's bookkeeping events are left to the Raw log.
  const items = useMemo(() => groupByIntent(built), [built]);
  const rawLeft = useMemo(() => built.reduce((sum, item) => sum + (item.type === "raw" ? item.events.length : 0), 0), [built]);
  const [toggled, setToggled] = useState<ReadonlyMap<string, boolean>>(() => new Map());
  const [follow, setFollow] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const virtualRef = useRef<Virtual | null>(null);
  const lastTop = useRef(0);
  const virtual = items.length > TRACE_VIRTUAL_THRESHOLD;
  // A group whose last call runs is the trace's live element (its clock): no typing dots after it.
  const lastItem = items[items.length - 1];
  const liveGroup = lastItem?.type === "group" && callRunning(lastItem.calls[lastItem.calls.length - 1].tool);
  const typing = context.working && status !== "failed" && !liveGroup;

  const onToggle = useMemo(
    () => (key: string, open: boolean) =>
      setToggled((previous) => {
        const next = new Map(previous);
        next.set(key, open);
        return next;
      }),
    [],
  );
  const isOpen = (item: NormalItem): string =>
    item.type === "group"
      ? `${toggled.get(item.key) ? 1 : 0}:${item.calls
          .filter((entry) => toggled.get(entry.key) ?? openByDefault(entry.tool))
          .map((entry) => entry.key)
          .join(",")}`
      : "";
  const render = (item: NormalItem) => <Item item={item} context={context} open={isOpen(item)} onToggle={onToggle} signature={signature(item)} />;

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
    <div className={cn("relative min-w-0", fill === "always" ? "flex min-h-0 flex-1 flex-col" : fill && "xl:flex xl:min-h-0 xl:flex-1 xl:flex-col")}>
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
          // Under 768 px the page goes on below the trace: at the trace's end, a swipe moves the page on.
          // 8 px of padding taken back by the margin: the rows' hover reaches past their text without being clipped.
          "relative -mx-2 max-h-[min(70vh,44rem)] min-h-48 overflow-y-auto overscroll-contain px-2 pt-2 pb-3 focus-visible:outline-offset-[-2px] max-md:max-h-[min(65dvh,44rem)] max-md:overscroll-auto",
          fill === "always" ? "max-h-none min-h-0 flex-1 max-md:max-h-none max-md:overscroll-contain" : fill && "xl:max-h-none xl:min-h-0 xl:flex-1",
        )}
        data-testid="trace"
        data-virtual={virtual || undefined}
        data-follow={follow}
      >
        {rawLeft > 0 ? (
          <p className="pb-1 text-xs text-fg-subtle" data-testid="trace-raw-left">
            {t("rawLeft", { count: rawLeft })}
          </p>
        ) : null}
        {empty ? (
          <p className="py-2 text-sm text-muted-foreground" data-testid="trace-empty">
            {events.length > 0 ? t("nothing") : context.active || status === "connecting" ? tLog("waiting") : tLog("none")}
          </p>
        ) : virtual ? (
          <VirtualItems items={items} render={render} scrollRef={scrollRef} virtualRef={virtualRef} />
        ) : (
          <ol className="flex flex-col">
            {items.map((item) => (
              <li key={item.key} className={cn("relative", lineClass(item))} data-testid="trace-item" data-type={item.type} data-seq={item.seq}>
                {render(item)}
              </li>
            ))}
          </ol>
        )}
        {typing && !empty ? <TraceTyping /> : null}
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
