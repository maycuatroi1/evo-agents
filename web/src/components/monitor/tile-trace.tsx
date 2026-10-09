"use client";

import { ArrowDown } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useDeferredValue, useLayoutEffect, useMemo, useRef, useState } from "react";

import { agentDigest, type DigestLine } from "@/components/inbox/agent-digest";
import type { RunEvent } from "@/components/runs/queries";
import { useTraceDuration } from "@/components/runs/trace-look";
import { buildTrace } from "@/components/runs/trace-model";
import type { LogStatus } from "@/components/runs/use-run-log";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { TILE_LINES } from "./model";

/**
 * The tail of a run's trace in a tile of the Monitor: the last things the run's Trace shows (`buildTrace`, read through
 * `agentDigest`: what the agent said, ran, planned and was told, one line each, moves between states left to the
 * pill), oldest first, on the terminal surface (`term-*`). It follows the newest line unless the person scrolled up,
 * when "Jump to the latest" shows. Its log region is quiet for screen readers (`aria-live="off"`): a wall of tiles
 * speaking at once would drown each other, so a tile says only when its run changes state.
 */

/** How close to the end (px) still counts as at the end. */
const END_SLACK = 24;
/** A decision id no line holds: the tail leaves none of its "Asked you" lines out. */
const NO_DECISION = -1;
/** A line's time as a terminal says it, 24-hour with seconds: narrow tiles keep their width for the words. */
const CLOCK = { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" } as const;

type View = { who: string; tone: string; text: string; meta: string | null; metaTone: string | null };

function useLineView(owner: string, viewer: string | null) {
  const t = useTranslations("runs.detail.trace");
  const duration = useTraceDuration();
  return (line: DigestLine): View => {
    switch (line.type) {
      case "agent":
        return {
          who: t("agent"),
          tone: "text-term-agent",
          text: line.text || (line.thoughtMs !== null && line.thoughtMs >= 1000 ? t("thought", { duration: duration(line.thoughtMs) }) : t("thoughtBrief")),
          meta: null,
          metaTone: null,
        };
      case "tool": {
        const failed = line.failed;
        const meta =
          line.exitCode !== null && line.exitCode !== 0
            ? t("exit", { code: line.exitCode })
            : line.status === "failed"
              ? t("status.failed")
              : line.ms !== null
                ? duration(line.ms)
                : line.status === "completed"
                  ? null
                  : t("status.in_progress");
        return { who: line.name ?? t(`kind.${line.kind}`), tone: "text-term-tool", text: line.arg ?? "", meta, metaTone: failed ? "text-term-error" : null };
      }
      case "plan":
        return { who: t("plan"), tone: "text-term-system", text: t("planCount", { done: line.done, total: line.total }), meta: null, metaTone: null };
      case "user": {
        const you = line.from !== null && line.from === viewer;
        const who = you ? t("you") : (line.from ?? t("someone"));
        const head = line.decisionId === null ? who : you ? t("youAnswered", { id: line.decisionId }) : t("answered", { login: who, id: line.decisionId });
        return { who: head, tone: "text-term-user", text: line.text, meta: null, metaTone: null };
      }
      case "system":
        return {
          who: "",
          tone: "",
          text: line.text,
          meta: null,
          metaTone: line.tone === "error" ? "text-term-error" : line.tone === "ok" ? "text-term-ok" : null,
        };
      case "ask":
        return { who: viewer === owner ? t("askedYou") : t("asked", { login: owner }), tone: "text-term-tool", text: line.question, meta: null, metaTone: null };
    }
  };
}

function TileLine({ line, view }: { line: DigestLine; view: View }) {
  const format = useFormatter();
  const at = new Date(line.at);
  const systemTone = line.type === "system" ? view.metaTone : null;
  return (
    <li className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-2" data-testid="tile-line" data-type={line.type}>
      <time dateTime={line.at} title={format.dateTime(at, { dateStyle: "full", timeStyle: "long" })} className="text-term-muted tabular-nums">
        {format.dateTime(at, CLOCK)}
      </time>
      <p className={cn("line-clamp-3 min-w-0 [overflow-wrap:anywhere]", systemTone ?? (line.type === "system" ? "text-term-muted" : "text-term-fg"))}>
        {view.who ? <span className={cn("font-medium", view.tone)}>{view.who}</span> : null}
        {view.who && view.text ? " " : null}
        {view.text}
        {view.meta ? <span className={cn("tabular-nums", view.metaTone ?? "text-term-muted")}>{`, ${view.meta}`}</span> : null}
      </p>
    </li>
  );
}

export function TileTrace({
  runId,
  events,
  status,
  owner,
  viewer,
  active,
  partial,
  error,
}: {
  runId: number;
  events: readonly RunEvent[];
  status: LogStatus;
  /** The login of the run's owner, whom the agent asks. */
  owner: string;
  /** The visitor's login, to say "You". */
  viewer: string | null;
  /** The run may still write events. */
  active: boolean;
  /** Events came before the first one read: the run's page has them. */
  partial: boolean;
  /** Why the run failed or was lost, as the hub says: the tail's last line. */
  error: string | null;
}) {
  const t = useTranslations("monitor.tile");
  const deferred = useDeferredValue(events);
  const lines = useMemo(() => agentDigest(buildTrace(deferred), NO_DECISION, TILE_LINES), [deferred]);
  const view = useLineView(owner, viewer);
  const [follow, setFollow] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const lastTop = useRef(0);

  const toEnd = () => {
    const element = scrollRef.current;
    if (!element) return;
    element.scrollTop = element.scrollHeight;
    lastTop.current = element.scrollTop;
  };

  // Keep the newest line in view while following.
  useLayoutEffect(() => {
    if (follow) toEnd();
  }, [lines, follow]);

  // The tile only ever scrolls the tail down, to its end; a move up away from the end is the person's.
  const onScroll = () => {
    const element = scrollRef.current;
    if (!element) return;
    const top = element.scrollTop;
    const atEnd = element.scrollHeight - top - element.clientHeight <= END_SLACK;
    if (follow && !atEnd && top < lastTop.current - 2) setFollow(false);
    else if (!follow && atEnd && top > lastTop.current) setFollow(true);
    lastTop.current = top;
  };

  let empty: string | null = null;
  if (lines.length === 0) {
    if (status === "failed") empty = t("unavailable");
    else if (!active) empty = error ? null : t("noTrace");
    else empty = events.length > 0 ? t("quiet") : t("waiting");
  }

  return (
    <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
      <div
        ref={scrollRef}
        role="log"
        aria-live="off"
        aria-label={t("trace", { id: runId })}
        tabIndex={0}
        onScroll={onScroll}
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain bg-term-bg px-3 py-2 font-mono text-xs leading-[18px] text-term-fg focus-visible:outline-offset-[-2px]"
        data-testid="tile-trace"
        data-follow={follow}
      >
        {partial ? (
          <p className="pb-1 text-term-muted" data-testid="tile-earlier">
            {t("earlier")}
          </p>
        ) : null}
        {empty !== null ? (
          <p className={status === "failed" ? "text-term-error" : "text-term-muted"} data-testid="tile-trace-empty">
            {empty}
          </p>
        ) : (
          <ol className="flex flex-col gap-1">
            {lines.map((line) => (
              <TileLine key={line.key} line={line} view={view(line)} />
            ))}
          </ol>
        )}
        {error && !active ? (
          <p className="pt-1 [overflow-wrap:anywhere] text-term-error" data-testid="tile-error-line">
            {error}
          </p>
        ) : null}
      </div>
      {!follow && empty === null ? (
        <Button
          type="button"
          variant="secondary"
          size="sm"
          className="absolute bottom-2 left-1/2 -translate-x-1/2 shadow-popover"
          onClick={() => {
            setFollow(true);
            toEnd();
          }}
          data-testid="tile-latest"
        >
          <ArrowDown aria-hidden="true" />
          {t("latest")}
        </Button>
      ) : null}
    </div>
  );
}
