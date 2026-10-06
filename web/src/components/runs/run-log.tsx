"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import {
  ArrowDownToLine,
  CircleCheck,
  Loader2,
  type LucideIcon,
  MessagesSquare,
  Pause,
  Play,
  Radio,
  RefreshCw,
  ScrollText,
  Settings2,
  SquareTerminal,
  WifiOff,
  Wrench,
} from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { memo, type ReactNode, type RefObject, useDeferredValue, useId, useLayoutEffect, useMemo, useRef, useState } from "react";

import { FacetGroup } from "@/components/data/facet-group";
import { SearchField } from "@/components/data/search-field";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";

import {
  filterLines,
  groupCounts,
  LOG_GROUPS,
  type LogGroup,
  type LogLine,
  splitMatches,
  VIRTUAL_THRESHOLD,
} from "./log-model";
import type { RunEventKind } from "./queries";
import type { LogStatus, RunLog } from "./use-run-log";

/**
 * A run's log as the page shows it: the lines of every event in seq order in a `role="log"` region (polite, so a
 * screen reader reads new lines as they come), filters by group, a search that highlights its matches, Follow (keep
 * the newest line in view; scrolling up turns it off, scrolling back to the end turns it on) and Pause (hold the
 * display while the stream goes on). Past 2,000 lines shown, only the rows in view are rendered.
 */

const KIND_TONE: Record<RunEventKind, string> = {
  agent_message_chunk: "text-term-agent",
  agent_thought_chunk: "text-term-agent",
  plan: "text-term-agent",
  user_message: "text-term-user",
  tool_call: "text-term-tool",
  tool_call_update: "text-term-tool",
  output: "text-term-muted",
  system: "text-term-system",
  state: "text-term-system",
  usage_update: "text-term-muted",
};

const GROUP_ICON: Record<LogGroup, LucideIcon> = {
  agent: MessagesSquare,
  tools: Wrench,
  output: SquareTerminal,
  system: Settings2,
};

/** How close to the end (px) still counts as at the end. */
const END_SLACK = 32;
/** A row's height before it is measured: one line of 12.5 px text at 1.6. */
const ROW_ESTIMATE = 22;

function textTone(line: LogLine): string {
  if (line.tone === "error") return "text-term-error";
  if (line.tone === "ok") return "text-term-ok";
  if (line.kind === "user_message") return "text-term-user";
  if (line.kind === "agent_thought_chunk" || line.kind === "output") return "text-term-muted";
  return "text-term-fg";
}

function Highlighted({ text, query }: { text: string; query: string }) {
  const parts = splitMatches(text, query);
  if (parts.length === 1) return <>{text}</>;
  return (
    <>
      {parts.map((part, index) =>
        index % 2 === 1 ? (
          <mark key={index} className="rounded-xs bg-term-mark px-0.5 text-term-bg">
            {part}
          </mark>
        ) : (
          part
        ),
      )}
    </>
  );
}

const LogRow = memo(function LogRow({ line, query, time, kind, notes }: { line: LogLine; query: string; time: string; kind: string; notes: string[] }) {
  return (
    <div
      className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-2.5 px-3 py-px hover:bg-term-row sm:grid-cols-[11ch_6.75rem_minmax(0,1fr)] sm:px-3.5"
      data-seq={line.seq}
      data-kind={line.kind}
      data-testid="log-line"
    >
      {/* 11ch holds the longest time of either locale, "12:42:05 AM", on one line in the mono log. */}
      <time dateTime={line.at} className="whitespace-nowrap text-term-muted tabular-nums">
        {time}
      </time>
      <span className={cn("truncate font-medium", KIND_TONE[line.kind])}>{kind}</span>
      <span className={cn("col-span-2 break-words whitespace-pre-wrap [overflow-wrap:anywhere] sm:col-span-1", textTone(line))}>
        <Highlighted text={line.text} query={query} />
        {notes.length ? <span className="text-term-muted"> [{notes.join("; ")}]</span> : null}
      </span>
    </div>
  );
});

function useRowProps() {
  const t = useTranslations("runs.detail.log");
  const format = useFormatter();
  return (line: LogLine) => {
    const notes: string[] = [];
    if (line.truncated) notes.push(t("truncated"));
    if (line.shortened) notes.push(t("shortened"));
    return {
      time: format.dateTime(new Date(line.at), { timeStyle: "medium" }),
      kind: t(`kind.${line.kind}`),
      notes,
    };
  };
}

function PlainRows({ lines, query }: { lines: readonly LogLine[]; query: string }) {
  const rowProps = useRowProps();
  return (
    <ol className="flex flex-col">
      {lines.map((line) => (
        <li key={line.seq}>
          <LogRow line={line} query={query} {...rowProps(line)} />
        </li>
      ))}
    </ol>
  );
}

type Virtual = ReturnType<typeof useVirtualizer<HTMLDivElement, HTMLLIElement>>;

function VirtualRows({
  lines,
  query,
  scrollRef,
  virtualRef,
}: {
  lines: readonly LogLine[];
  query: string;
  scrollRef: RefObject<HTMLDivElement | null>;
  /** Where the card finds the virtualizer, to scroll to the newest line. */
  virtualRef: RefObject<Virtual | null>;
}) {
  const rowProps = useRowProps();
  // The app does not use the React Compiler, which this rule is about; the virtualizer re-renders through its own state.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer<HTMLDivElement, HTMLLIElement>({
    count: lines.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_ESTIMATE,
    getItemKey: (index) => lines[index].seq,
    overscan: 24,
  });
  useLayoutEffect(() => {
    virtualRef.current = virtualizer;
    return () => {
      virtualRef.current = null;
    };
  }, [virtualRef, virtualizer]);
  return (
    <ol className="relative" style={{ height: virtualizer.getTotalSize() }}>
      {virtualizer.getVirtualItems().map((item) => {
        const line = lines[item.index];
        return (
          <li
            key={item.key}
            ref={virtualizer.measureElement}
            data-index={item.index}
            className="absolute inset-x-0 top-0"
            style={{ transform: `translateY(${item.start}px)` }}
          >
            <LogRow line={line} query={query} {...rowProps(line)} />
          </li>
        );
      })}
    </ol>
  );
}

const STATUS_LOOK: Record<LogStatus, { icon: LucideIcon; variant: "info" | "warning" | "success" | "destructive" | "secondary"; spin?: boolean }> = {
  connecting: { icon: Loader2, variant: "secondary", spin: true },
  live: { icon: Radio, variant: "info" },
  reconnecting: { icon: RefreshCw, variant: "warning", spin: true },
  polling: { icon: RefreshCw, variant: "warning" },
  ended: { icon: CircleCheck, variant: "success" },
  failed: { icon: WifiOff, variant: "destructive" },
};

export function LogStatusBadge({ status }: { status: LogStatus }) {
  const t = useTranslations("runs.detail.log.status");
  const { icon: Icon, variant, spin } = STATUS_LOOK[status];
  return (
    <Badge variant={variant} data-testid="log-status" data-status={status}>
      <Icon className={cn(spin && "animate-spin motion-reduce:animate-none")} aria-hidden="true" />
      {t(status)}
    </Badge>
  );
}

type SessionTab = "log" | "terminal";

export function RunLogCard({
  runId,
  log,
  active,
  composer,
  terminal = null,
}: {
  runId: number;
  log: RunLog;
  /** The run may still write events: an empty log says it waits for them. */
  active: boolean;
  composer: ReactNode;
  /**
   * The Terminal tab's panel, for the owner of the run and of its worker (run-terminal.tsx), given whether it is the
   * tab shown. Without it the card is the log alone. Both panels stay mounted, so switching tabs keeps the terminal's
   * session and the log's place.
   */
  terminal?: ((shown: boolean) => ReactNode) | null;
}) {
  const t = useTranslations("runs.detail.log");
  const tTabs = useTranslations("runs.detail.terminal.tabs");
  const ids = useId();
  const [tab, setTab] = useState<SessionTab>("log");
  const [group, setGroup] = useState<LogGroup | null>(null);
  const [query, setQuery] = useState("");
  const [follow, setFollow] = useState(true);
  const [frozenAt, setFrozenAt] = useState<number | null>(null);
  const deferredQuery = useDeferredValue(query);

  const all = log.lines;
  const base = useMemo(() => (frozenAt === null ? all : all.slice(0, frozenAt)), [all, frozenAt]);
  const counts = useMemo(() => groupCounts(base), [base]);
  const shown = useMemo(() => filterLines(base, group, deferredQuery), [base, group, deferredQuery]);
  const waiting = frozenAt === null ? 0 : all.length - frozenAt;
  const virtual = shown.length > VIRTUAL_THRESHOLD;

  const scrollRef = useRef<HTMLDivElement>(null);
  const virtualRef = useRef<Virtual | null>(null);
  const lastTop = useRef(0);

  // Keep the newest line in view while following, and when the Log tab is shown again (hidden, the log cannot scroll).
  useLayoutEffect(() => {
    const element = scrollRef.current;
    if (!follow || !element || shown.length === 0 || tab !== "log") return;
    if (virtual && virtualRef.current) virtualRef.current.scrollToIndex(shown.length - 1, { align: "end" });
    else element.scrollTop = element.scrollHeight;
    lastTop.current = element.scrollTop;
  }, [shown, follow, virtual, tab]);

  // The page only ever scrolls the log down, to its end; a move up away from the end is the person's.
  const onScroll = () => {
    const element = scrollRef.current;
    if (!element) return;
    const top = element.scrollTop;
    const atEnd = element.scrollHeight - top - element.clientHeight <= END_SLACK;
    if (follow && !atEnd && top < lastTop.current - 2) setFollow(false); // the person scrolled up
    else if (!follow && atEnd && top > lastTop.current) setFollow(true); // and back down to the end
    lastTop.current = top;
  };

  const filtered = group !== null || deferredQuery.trim() !== "";
  const empty = shown.length === 0;

  const logState = (
    <>
      <span role="status" className="inline-flex">
        <LogStatusBadge status={log.status} />
      </span>
      <span className="ml-auto text-xs text-muted-foreground tabular-nums" data-testid="log-count">
        {filtered ? t("countFiltered", { shown: shown.length, total: base.length }) : t("count", { total: base.length })}
      </span>
    </>
  );

  const logBody = (
    <>
      <div className="flex flex-col gap-3 border-b px-4 py-3">
        <FacetGroup
          label={t("filter")}
          options={[
            { value: null, label: t("groups.all"), count: base.length },
            ...LOG_GROUPS.map((value) => ({ value, label: t(`groups.${value}`), icon: GROUP_ICON[value], count: counts[value] })),
          ]}
          selected={group}
          onSelect={(value) => setGroup(value as LogGroup | null)}
          countLabel={(count) => t("lines", { count })}
          testId="log-facets"
        />
        <div className="flex flex-wrap items-center gap-2">
          <SearchField
            value={query}
            onCommit={setQuery}
            debounce={150}
            label={t("search.label")}
            placeholder={t("search.placeholder")}
            clearLabel={t("search.clear")}
            className="min-w-48 flex-1 sm:max-w-xs"
            testId="log-search"
          />
          <Button
            type="button"
            variant="outline"
            aria-pressed={follow}
            onClick={() => setFollow((value) => !value)}
            className="aria-pressed:border-brand aria-pressed:bg-surface-selected aria-pressed:text-foreground"
            data-testid="log-follow"
          >
            <ArrowDownToLine aria-hidden="true" />
            {t("follow")}
          </Button>
          <Button
            type="button"
            variant="outline"
            aria-pressed={frozenAt !== null}
            onClick={() => setFrozenAt((value) => (value === null ? all.length : null))}
            data-testid="log-pause"
          >
            {frozenAt === null ? <Pause aria-hidden="true" /> : <Play aria-hidden="true" />}
            {frozenAt === null ? t("pause") : t("resume")}
          </Button>
        </div>
      </div>
      <div
        ref={scrollRef}
        role="log"
        aria-live="polite"
        aria-relevant="additions"
        aria-label={t("label", { id: runId })}
        aria-busy={log.status === "connecting" || undefined}
        tabIndex={0}
        onScroll={onScroll}
        className="h-[min(60vh,32rem)] min-h-64 overflow-y-auto overscroll-contain bg-term-bg py-2 font-mono text-[12.5px] leading-[1.6] text-term-fg [font-variant-ligatures:none] focus-visible:outline-offset-[-2px]"
        data-testid="log-lines"
        data-virtual={virtual || undefined}
      >
        {empty ? (
          <p className="px-4 py-2 text-term-muted" data-testid="log-empty">
            {filtered ? t("noMatch") : active || log.status === "connecting" ? t("waiting") : t("none")}
          </p>
        ) : virtual ? (
          <VirtualRows lines={shown} query={deferredQuery} scrollRef={scrollRef} virtualRef={virtualRef} />
        ) : (
          <PlainRows lines={shown} query={deferredQuery} />
        )}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border-t px-4 py-2 text-xs text-muted-foreground">
        <span aria-live="polite" data-testid="log-paused">
          {frozenAt !== null ? t("paused", { count: waiting }) : log.status === "polling" ? t("pollingHint") : ""}
        </span>
        <span>{t("kept")}</span>
      </div>
      {composer}
    </>
  );

  if (!terminal) {
    return (
      <section aria-labelledby={`${ids}-title`} className="flex min-w-0 flex-col rounded-md border bg-card shadow-raised" data-testid="run-log">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b px-4 py-3">
          <h2 id={`${ids}-title`} className="text-[15px] leading-[22px] font-semibold">
            {t("title")}
          </h2>
          {logState}
        </div>
        {logBody}
      </section>
    );
  }

  return (
    <Tabs value={tab} onValueChange={(value) => setTab(value === "terminal" ? "terminal" : "log")} className="gap-0" asChild>
      <section
        aria-labelledby={`${ids}-title`}
        className="flex min-w-0 flex-col rounded-md border bg-card shadow-raised"
        data-testid="run-log"
        data-tab={tab}
      >
        <h2 id={`${ids}-title`} className="sr-only">
          {tTabs("heading", { id: runId })}
        </h2>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b px-4">
          <TabsList aria-label={tTabs("label")} className="w-auto border-b-0">
            <TabsTrigger value="log" className="h-12" data-testid="run-tab-log">
              <ScrollText aria-hidden="true" />
              {tTabs("log")}
            </TabsTrigger>
            <TabsTrigger value="terminal" className="h-12" data-testid="run-tab-terminal">
              <SquareTerminal aria-hidden="true" />
              {tTabs("terminal")}
            </TabsTrigger>
          </TabsList>
          {tab === "log" ? logState : null}
        </div>
        <TabsContent value="log" forceMount className="gap-0 data-[state=inactive]:hidden">
          {logBody}
        </TabsContent>
        <TabsContent value="terminal" forceMount className="gap-0 data-[state=inactive]:hidden">
          {terminal(tab === "terminal")}
        </TabsContent>
      </section>
    </Tabs>
  );
}
