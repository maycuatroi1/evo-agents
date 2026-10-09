"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import {
  ArrowDownToLine,
  ChevronDown,
  Layers,
  type LucideIcon,
  MessageCircle,
  MessagesSquare,
  Pause,
  Play,
  ListTree,
  ScrollText,
  Settings2,
  SquareTerminal,
  Terminal,
  Wrench,
} from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { memo, type ReactNode, type RefObject, useDeferredValue, useId, useLayoutEffect, useMemo, useRef, useState } from "react";

import { FacetGroup } from "@/components/data/facet-group";
import { SearchField } from "@/components/data/search-field";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useIsMobile } from "@/hooks/use-mobile";
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

/**
 * The log's groups on a phone: one 44 px button with the group shown (named "Show: Tools"), opening a menu of the
 * groups with their line counts, instead of a row of chips that would wrap over three lines. Under the search field,
 * beside Follow and Pause.
 */
function GroupMenu({
  group,
  counts,
  total,
  onSelect,
}: {
  group: LogGroup | null;
  counts: Record<LogGroup, number>;
  total: number;
  onSelect: (group: LogGroup | null) => void;
}) {
  const t = useTranslations("runs.detail.log");
  const format = useFormatter();
  const options: { value: LogGroup | null; label: string; icon: LucideIcon; count: number }[] = [
    { value: null, label: t("groups.all"), icon: Layers, count: total },
    ...LOG_GROUPS.map((value) => ({ value, label: t(`groups.${value}`), icon: GROUP_ICON[value], count: counts[value] })),
  ];
  const shown = options.find((option) => option.value === group) ?? options[0];
  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="outline"
          className={cn("min-w-0 shrink-0", group !== null && "border-brand bg-surface-selected text-foreground")}
          // "Show: Tools" to a screen reader; the group's name and icon on screen.
          aria-label={t("groupMenu", { group: shown.label })}
          data-testid="log-group-menu"
          data-group={group ?? "all"}
        >
          <shown.icon aria-hidden="true" />
          {shown.label}
          <ChevronDown aria-hidden="true" className="text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-56">
        <DropdownMenuLabel>{t("filter")}</DropdownMenuLabel>
        <DropdownMenuRadioGroup
          value={group ?? ""}
          onValueChange={(value) => onSelect((LOG_GROUPS as readonly string[]).includes(value) ? (value as LogGroup) : null)}
        >
          {options.map((option) => (
            <DropdownMenuRadioItem key={option.value ?? ""} value={option.value ?? ""} data-testid={`log-group-${option.value ?? "all"}`}>
              <option.icon aria-hidden="true" />
              {option.label}
              <span className="ml-auto pl-3 text-xs text-fg-subtle tabular-nums">
                <span aria-hidden="true">{format.number(option.count)}</span>
                <span className="sr-only">{t("lines", { count: option.count })}</span>
              </span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** The stream's state as the kit's live state beside the tabs: a dot in its tone (pulsing while live) and a word. */
const STATUS_DOT: Record<LogStatus, string> = {
  connecting: "bg-neutral-solid",
  live: "bg-success-solid",
  reconnecting: "bg-attention-solid",
  polling: "bg-attention-solid",
  ended: "bg-neutral-solid",
  failed: "bg-danger-solid",
};

function LogStreamState({ status }: { status: LogStatus }) {
  const t = useTranslations("runs.detail.log.status");
  return (
    <span className="inline-flex items-center gap-2 text-xs whitespace-nowrap text-fg-subtle" data-testid="log-status" data-status={status}>
      <span className="relative inline-flex size-2 shrink-0" aria-hidden="true">
        <span className={cn("size-2 rounded-full", STATUS_DOT[status])} />
        {status === "live" ? <span className={cn("absolute inset-0 animate-live-ping rounded-full", STATUS_DOT.live)} /> : null}
      </span>
      {t(status)}
    </span>
  );
}

export type SessionTab = "chat" | "trace" | "log" | "terminal";

export const SESSION_TABS: readonly SessionTab[] = ["chat", "trace", "log", "terminal"];

/**
 * The card of a run's session: the Chat of an author run (first), the Trace (the kit's AgentTrace), the Raw log and,
 * for the owner of the run and of its worker, the Terminal, as tabs. Every panel stays mounted, so switching tabs keeps the terminal's session, the
 * log's place and the trace's open rows. The composer sits under the Trace and the Raw log.
 */
export function RunLogCard({
  runId,
  log,
  active,
  composer,
  chat = null,
  trace = null,
  terminal = null,
  frozen,
  tab: heldTab,
  onTabChange,
  fill = false,
}: {
  runId: number;
  log: RunLog;
  /** The run may still write events: an empty log says it waits for them. */
  active: boolean;
  composer: ReactNode;
  /** The Chat tab's panel of an author run, given whether it is the tab shown; first, and shown first, when given. */
  chat?: ((shown: boolean) => ReactNode) | null;
  /** The Trace tab's panel, given whether it is the tab shown. Without it and a terminal, the card is the log alone. */
  trace?: ((shown: boolean) => ReactNode) | null;
  /**
   * The Terminal tab's panel, for the owner of the run and of its worker (run-terminal.tsx), given whether it is the
   * tab shown.
   */
  terminal?: ((shown: boolean) => ReactNode) | null;
  /**
   * Pause held by the page (the line count shown when paused, null while live), so the top bar's LiveIndicator can say
   * Paused and resume it; without it the card holds Pause itself.
   */
  frozen?: { at: number | null; set: (at: number | null) => void };
  /** The tab shown, when the page holds it (a decision's Take over shows the Terminal tab); the card holds it otherwise. */
  tab?: SessionTab;
  onTabChange?: (tab: SessionTab) => void;
  /**
   * From the xl breakpoint, the card takes the height its parent gives (the run page's split layout, as tall as the
   * window allows) and the panel shown fills it, above the composer; below xl, and without it, the panels keep their
   * own heights.
   */
  fill?: boolean;
}) {
  const t = useTranslations("runs.detail.log");
  const tTabs = useTranslations("runs.detail.terminal.tabs");
  const format = useFormatter();
  const ids = useId();
  const phone = useIsMobile();
  const first: SessionTab = chat ? "chat" : trace ? "trace" : "log";
  const [ownTab, setOwnTab] = useState<SessionTab>(first);
  const wanted = heldTab ?? ownTab;
  // A tab the card does not have (the Terminal once it is not offered, the Trace or the Chat without one) shows the
  // first it has.
  const tab: SessionTab =
    (wanted === "terminal" && !terminal) || (wanted === "trace" && !trace) || (wanted === "chat" && !chat) ? first : wanted;
  const setTab = onTabChange ?? setOwnTab;
  const [group, setGroup] = useState<LogGroup | null>(null);
  const [query, setQuery] = useState("");
  const [follow, setFollow] = useState(true);
  const [ownFrozenAt, setOwnFrozenAt] = useState<number | null>(null);
  const frozenAt = frozen ? frozen.at : ownFrozenAt;
  const setFrozenAt = frozen ? frozen.set : setOwnFrozenAt;
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

  // Keep the newest line in view while following, and when the Raw log tab is shown again (hidden, it cannot scroll).
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
  // The panel shown fills the card above the composer; its own scroll region takes what the bars leave.
  const panelFill = fill ? "xl:min-h-0 xl:flex-1" : undefined;

  const status = (
    <span role="status" className="inline-flex">
      <LogStreamState status={log.status} />
    </span>
  );
  const count = (
    <span className="text-xs text-muted-foreground tabular-nums" data-testid="log-count">
      {filtered ? t("countFiltered", { shown: shown.length, total: base.length }) : t("count", { total: base.length })}
    </span>
  );

  const logBody = (
    <>
      <div className="flex flex-col gap-3 border-b px-4 py-3">
        {phone ? null : (
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
        )}
        <div className="flex flex-wrap items-center gap-2">
          {phone ? <GroupMenu group={group} counts={counts} total={base.length} onSelect={setGroup} /> : null}
          <SearchField
            value={query}
            onCommit={setQuery}
            debounce={150}
            label={t("search.label")}
            placeholder={t("search.placeholder")}
            clearLabel={t("search.clear")}
            className="min-w-48 flex-1 sm:max-w-xs max-md:order-first max-md:basis-full"
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
            onClick={() => setFrozenAt(frozenAt === null ? all.length : null)}
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
        className={cn(
          "h-[min(60vh,32rem)] min-h-64 overflow-y-auto overscroll-contain bg-term-bg py-2 font-mono text-[12.5px] leading-[1.6] text-term-fg [font-variant-ligatures:none] focus-visible:outline-offset-[-2px]",
          fill && "xl:h-auto xl:min-h-0 xl:flex-1",
        )}
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
    </>
  );

  if (!chat && !trace && !terminal) {
    return (
      <section
        aria-labelledby={`${ids}-title`}
        className={cn("flex min-w-0 flex-col rounded-md border bg-card shadow-raised", fill && "xl:min-h-0 xl:flex-1")}
        data-testid="run-log"
      >
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b px-4 py-3">
          <h2 id={`${ids}-title`} className="text-[15px] leading-[22px] font-semibold">
            {t("title")}
          </h2>
          <div className="ml-auto flex items-center gap-3">
            {count}
            {status}
          </div>
        </div>
        {logBody}
        {composer}
      </section>
    );
  }

  return (
    <Tabs
      value={tab}
      onValueChange={(value) => setTab((SESSION_TABS as readonly string[]).includes(value) ? (value as SessionTab) : "log")}
      className="gap-0"
      asChild
    >
      <section
        aria-labelledby={`${ids}-title`}
        className={cn("flex min-w-0 flex-col rounded-md border bg-card shadow-raised", fill && "xl:min-h-0 xl:flex-1")}
        data-testid="run-log"
        data-tab={tab}
      >
        <h2 id={`${ids}-title`} className="sr-only">
          {tTabs("heading", { id: runId })}
        </h2>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b px-4">
          {/* The bar's border is the line here: the list reaches 1 px into it, so the underline covers it. */}
          <TabsList aria-label={tTabs("label")} className="-mb-px w-auto max-w-full self-end shadow-none">
            {chat ? (
              <TabsTrigger value="chat" className="h-12" data-testid="run-tab-chat">
                <MessageCircle aria-hidden="true" />
                {tTabs("chat")}
              </TabsTrigger>
            ) : null}
            {trace ? (
              <TabsTrigger value="trace" className="h-12" data-testid="run-tab-trace">
                <ListTree aria-hidden="true" />
                {tTabs("trace")}
              </TabsTrigger>
            ) : null}
            <TabsTrigger value="log" className="h-12" data-testid="run-tab-log">
              <ScrollText aria-hidden="true" />
              {tTabs("log")}
              {all.length > 0 ? (
                <>
                  <span
                    aria-hidden="true"
                    className="rounded-full bg-muted px-1.5 text-[11px] leading-[18px] font-medium text-muted-foreground tabular-nums"
                    data-testid="run-tab-log-count"
                  >
                    {format.number(all.length)}
                  </span>
                  <span className="sr-only">, {t("lines", { count: all.length })}</span>
                </>
              ) : null}
            </TabsTrigger>
            {terminal ? (
              <TabsTrigger value="terminal" className="h-12" data-testid="run-tab-terminal">
                <Terminal aria-hidden="true" />
                {tTabs("terminal")}
              </TabsTrigger>
            ) : null}
          </TabsList>
          {/* The kit's tab bar: what the stream is doing at its right end, the Raw log's line count before it. */}
          {tab !== "terminal" ? (
            <div className="ml-auto flex items-center gap-3">
              {tab === "log" ? count : null}
              {status}
            </div>
          ) : null}
        </div>
        {chat ? (
          <TabsContent value="chat" forceMount className={cn("gap-0 data-[state=inactive]:hidden", panelFill)}>
            {chat(tab === "chat")}
          </TabsContent>
        ) : null}
        {trace ? (
          <TabsContent value="trace" forceMount className={cn("gap-0 data-[state=inactive]:hidden", panelFill)}>
            {trace(tab === "trace")}
          </TabsContent>
        ) : null}
        <TabsContent value="log" forceMount className={cn("gap-0 data-[state=inactive]:hidden", panelFill)}>
          {logBody}
        </TabsContent>
        {terminal ? (
          <TabsContent value="terminal" forceMount className={cn("gap-0 data-[state=inactive]:hidden", panelFill)}>
            {terminal(tab === "terminal")}
          </TabsContent>
        ) : null}
        {tab !== "terminal" && tab !== "chat" ? composer : null}
      </section>
    </Tabs>
  );
}
