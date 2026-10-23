"use client";

import { ArrowDownToLine, Globe, Images, ListTree, MessageCircle, Pause, Play, ScrollText, Terminal } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useDeferredValue, useId, useLayoutEffect, useMemo, useRef, useState } from "react";

import { FacetGroup } from "@/components/data/facet-group";
import { SearchField } from "@/components/data/search-field";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useIsMobile } from "@/hooks/use-mobile";
import { cn } from "@/lib/utils";

import { GROUP_ICON, GroupMenu } from "./log-group-menu";
import { filterLines, groupCounts, LOG_GROUPS, type LogGroup, VIRTUAL_THRESHOLD } from "./log-model";
import { PlainRows, type Virtual, VirtualRows } from "./log-rows";
import { LogStreamState } from "./log-stream-state";
import type { RunLog } from "./use-run-log";

/**
 * A run's log as the page shows it: the lines of every event in seq order in a `role="log"` region (polite, so a
 * screen reader reads new lines as they come), filters by group, a search that highlights its matches, Follow (keep
 * the newest line in view; scrolling up turns it off, scrolling back to the end turns it on) and Pause (hold the
 * display while the stream goes on). Past 2,000 lines shown, only the rows in view are rendered.
 */

/** How close to the end (px) still counts as at the end. */
const END_SLACK = 32;

/** A tab's count: a round pill drawn for the eye, said in words after the tab's name. */
function TabCount({ count, label, testId }: { count: number; label: string; testId: string }) {
  const format = useFormatter();
  return (
    <>
      <span aria-hidden="true" className="rounded-full bg-muted px-1.5 text-[11px] leading-[18px] font-medium text-muted-foreground tabular-nums" data-testid={testId}>
        {format.number(count)}
      </span>
      <span className="sr-only">, {label}</span>
    </>
  );
}

export type SessionTab = "chat" | "trace" | "outputs" | "log" | "terminal" | "browser";

export const SESSION_TABS: readonly SessionTab[] = ["chat", "trace", "outputs", "log", "terminal", "browser"];

/**
 * The card of a run's session: the Chat of an author run (first), the Trace (the kit's AgentTrace), the Outputs once
 * the run has files (run-outputs.tsx), the Raw log, for the owner of the run and of its worker the Terminal, and for
 * the dispatcher of a run started with a browser the Browser, as tabs. Every panel stays mounted, so switching tabs
 * keeps the terminal's session, the log's place and the trace's open rows. The composer sits under the Trace and the
 * Raw log.
 */
export function RunLogCard({
  runId,
  log,
  active,
  composer,
  chat = null,
  trace = null,
  outputs = null,
  terminal = null,
  browser = null,
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
  /** The Outputs tab's panel and how many files the run holds, given once it holds any. */
  outputs?: { count: number; panel: () => ReactNode } | null;
  /**
   * The Terminal tab's panel, for the owner of the run and of its worker (run-terminal.tsx), given whether it is the
   * tab shown.
   */
  terminal?: ((shown: boolean) => ReactNode) | null;
  /** The Browser tab's panel, for the dispatcher of a run started with a browser (run-browser.tsx), given whether it is shown. */
  browser?: ((shown: boolean) => ReactNode) | null;
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
  fill?: boolean | "always";
}) {
  const t = useTranslations("runs.detail.log");
  const tTabs = useTranslations("runs.detail.terminal.tabs");
  const tOutputs = useTranslations("runs.detail.outputs");
  const ids = useId();
  const phone = useIsMobile();
  const first: SessionTab = chat ? "chat" : trace ? "trace" : "log";
  const [ownTab, setOwnTab] = useState<SessionTab>(first);
  const wanted = heldTab ?? ownTab;
  // A tab the card does not have (the Terminal or the Browser once it is not offered, the Trace or the Chat without
  // one) shows the first it has.
  const missing: Record<SessionTab, boolean> = { chat: !chat, trace: !trace, outputs: !outputs, log: false, terminal: !terminal, browser: !browser };
  const tab: SessionTab = missing[wanted] ? first : wanted;
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
  // "always": at every width, for a phone's trace screen, which is as tall as the window.
  const panelFill = fill === "always" ? "min-h-0 flex-1" : fill ? "xl:min-h-0 xl:flex-1" : undefined;

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
      <div className="flex flex-col gap-3 border-b py-3">
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
          "mt-3 h-[min(60vh,32rem)] min-h-64 overflow-y-auto overscroll-contain rounded-md bg-term-bg py-2 font-mono text-[12.5px] leading-[1.6] text-term-fg [font-variant-ligatures:none] focus-visible:outline-offset-[-2px]",
          fill === "always" ? "h-auto min-h-0 flex-1" : fill && "xl:h-auto xl:min-h-0 xl:flex-1",
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
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 py-2 text-xs text-muted-foreground">
        <span aria-live="polite" data-testid="log-paused">
          {frozenAt !== null ? t("paused", { count: waiting }) : log.status === "polling" ? t("pollingHint") : ""}
        </span>
        <span>{t("kept")}</span>
      </div>
    </>
  );

  if (!chat && !trace && !terminal && !browser && !outputs) {
    return (
      <section
        aria-labelledby={`${ids}-title`}
        className={cn("flex min-w-0 flex-col", panelFill)}
        data-testid="run-log"
      >
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b py-3">
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
        className={cn("flex min-w-0 flex-col", panelFill)}
        data-testid="run-log"
        data-tab={tab}
      >
        <h2 id={`${ids}-title`} className="sr-only">
          {tTabs("heading", { id: runId })}
        </h2>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b">
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
            {outputs ? (
              <TabsTrigger value="outputs" className="h-12" data-testid="run-tab-outputs">
                <Images aria-hidden="true" />
                {tTabs("outputs")}
                <TabCount count={outputs.count} label={tOutputs("count", { count: outputs.count })} testId="run-tab-outputs-count" />
              </TabsTrigger>
            ) : null}
            <TabsTrigger value="log" className="h-12" data-testid="run-tab-log">
              <ScrollText aria-hidden="true" />
              {tTabs("log")}
              {all.length > 0 ? <TabCount count={all.length} label={t("lines", { count: all.length })} testId="run-tab-log-count" /> : null}
            </TabsTrigger>
            {terminal ? (
              <TabsTrigger value="terminal" className="h-12" data-testid="run-tab-terminal">
                <Terminal aria-hidden="true" />
                {tTabs("terminal")}
              </TabsTrigger>
            ) : null}
            {browser ? (
              <TabsTrigger value="browser" className="h-12" data-testid="run-tab-browser">
                <Globe aria-hidden="true" />
                {tTabs("browser")}
              </TabsTrigger>
            ) : null}
          </TabsList>
          {/* The kit's tab bar: what the stream is doing at its right end, the Raw log's line count before it. */}
          {tab !== "terminal" && tab !== "browser" ? (
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
        {outputs ? (
          <TabsContent value="outputs" forceMount className={cn("gap-0 data-[state=inactive]:hidden", panelFill)}>
            {outputs.panel()}
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
        {browser ? (
          <TabsContent value="browser" forceMount className={cn("gap-0 data-[state=inactive]:hidden", panelFill)}>
            {browser(tab === "browser")}
          </TabsContent>
        ) : null}
        {tab === "trace" || tab === "log" ? composer : null}
      </section>
    </Tabs>
  );
}
