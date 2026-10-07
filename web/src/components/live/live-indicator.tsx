"use client";

import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { Play, RotateCw } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useEffect, useReducer, useState, useSyncExternalStore } from "react";

import { useNow } from "@/components/kg/use-now";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { type LiveEntry, type LiveQueryEntry, useLiveStore } from "./live-context";
import {
  INITIAL_LIVE,
  type LiveMachine,
  type LiveSignal,
  type LiveState,
  mergeSignals,
  querySignal,
  secondsSince,
  secondsUntil,
  stepLive,
} from "./live-model";

const NO_ENTRIES: readonly LiveEntry[] = [];
const noop = () => () => {};

const DOT: Record<LiveState, string> = {
  live: "bg-success-solid",
  reconnecting: "bg-attention-solid",
  paused: "bg-neutral-solid",
  offline: "bg-danger-solid",
};

/** A registered query's signal from the cache: none while it has no data yet or does not poll. */
function signalOfQuery(client: QueryClient, entry: LiveQueryEntry): LiveSignal | null {
  const query = client.getQueryCache().get(entry.queryHash);
  if (!query || query.state.dataUpdatedAt === 0) return null;
  const interval = entry.interval();
  const pollMs = typeof interval === "function" ? interval(query as never) : interval;
  if (typeof pollMs !== "number" || pollMs <= 0) return null;
  return querySignal(query.state, pollMs);
}

/** The registered entries, and a render whenever one of their queries changes in the cache. */
function useEntries(): readonly LiveEntry[] {
  const store = useLiveStore();
  const client = useQueryClient();
  const entries = useSyncExternalStore(store?.subscribe ?? noop, store?.snapshot ?? (() => NO_ENTRIES), () => NO_ENTRIES);
  const [, changed] = useReducer((count: number) => count + 1, 0);
  useEffect(() => {
    const hashes = new Set(entries.flatMap((entry) => (entry.kind === "query" ? [entry.queryHash] : [])));
    if (hashes.size === 0) return;
    return client.getQueryCache().subscribe((event) => {
      if (hashes.has(event.query.queryHash)) changed();
    });
  }, [client, entries]);
  return entries;
}

/** "3s", "1.5s", "2 min": how long, in the indicator's short words. */
function useDuration() {
  const t = useTranslations("live.duration");
  const format = useFormatter();
  return (ms: number) =>
    ms < 60_000
      ? t("seconds", { value: format.number(ms / 1000, { maximumFractionDigits: 1 }) })
      : t("minutes", { value: format.number(Math.round(ms / 60_000)) });
}

/** "just now", "12s ago", "3 min ago", "2 h ago". */
function useAgo() {
  const t = useTranslations("live.ago");
  return (seconds: number) =>
    seconds < 1
      ? t("now")
      : seconds < 60
        ? t("seconds", { count: seconds })
        : seconds < 3600
          ? t("minutes", { count: Math.floor(seconds / 60) })
          : t("hours", { count: Math.floor(seconds / 3600) });
}

/**
 * The kit's LiveIndicator in the top bar (web/DESIGN.md, Live state): whether the page is current, from what the page
 * registered (its main query's dataUpdatedAt, fetchStatus and failures; a run's event stream; the log's Pause). Live
 * says when the last update came, Reconnecting when the next try is due and how the page reads meanwhile, Paused who
 * paused it with Resume, Offline the time of the last update with Retry now. One clock (`useNow`) ticks every second
 * for every caller. The state is a `role="status"` region holding the state's name only, so a screen reader hears a
 * change of state and not every tick of the clock. Nothing shows on a page that registered nothing.
 */
export function LiveIndicator({ className }: { className?: string }) {
  const t = useTranslations("live");
  const client = useQueryClient();
  const entries = useEntries();
  const now = useNow(entries.length > 0);
  const duration = useDuration();
  const ago = useAgo();

  const signals: LiveSignal[] = [];
  for (const entry of entries) {
    const signal = entry.kind === "query" ? signalOfQuery(client, entry) : entry.signal;
    if (signal) signals.push(signal);
  }
  const signal = mergeSignals(signals);

  const [machine, setMachine] = useState<LiveMachine>(INITIAL_LIVE);
  const next = signal && now !== null ? stepLive(machine, signal, now) : INITIAL_LIVE;
  if (next !== machine) setMachine(next);

  if (!signal || now === null) return null;
  const state = next.state;

  const retry = () => {
    for (const entry of entries) {
      if (entry.kind === "query") void client.refetchQueries({ queryKey: entry.queryKey, exact: true });
      else entry.retry?.();
    }
  };
  const resume = () => {
    for (const entry of entries) if (entry.kind === "signal") entry.resume?.();
  };

  let detail: string;
  if (state === "live") {
    detail = t("detail.updated", { ago: ago(secondsSince(signal.updatedAt, now)) });
  } else if (state === "paused") {
    detail = t("detail.paused");
  } else if (state === "offline") {
    detail = signal.updatedAt > 0 ? t("detail.lastUpdate", { ago: ago(secondsSince(signal.updatedAt, now)) }) : t("detail.noUpdate");
  } else if (signal.transport === "stream") {
    const retryIn = secondsUntil(signal.retryAt, now);
    detail =
      signal.pollMs !== null && retryIn !== null
        ? t("detail.streamRetry", { retry: duration(retryIn * 1000), poll: duration(signal.pollMs) })
        : t("detail.streamReconnecting");
  } else {
    const retryIn = secondsUntil(signal.retryAt, now);
    const poll = duration(signal.pollMs ?? 0);
    detail = retryIn === null || retryIn === 0 ? t("detail.retryingNow", { poll }) : t("detail.retryIn", { retry: duration(retryIn * 1000), poll });
  }

  return (
    <div
      className={cn("flex min-w-0 items-center gap-1", className)}
      data-testid="live-indicator"
      data-state={state}
      data-transport={signal.transport}
    >
      <div className="flex h-7 min-w-0 items-center gap-2 rounded-sm px-2.5 text-xs whitespace-nowrap text-fg-subtle">
        <span className="relative inline-flex size-2 shrink-0" aria-hidden="true">
          <span className={cn("size-2 rounded-full", DOT[state])} />
          {state === "live" ? <span className={cn("absolute inset-0 animate-live-ping rounded-full", DOT.live)} /> : null}
        </span>
        <span role="status" className="font-medium text-foreground" data-testid="live-state">
          <span className="sr-only">{t("label")} </span>
          {t(`state.${state}`)}
        </span>
        <span className="hidden truncate md:inline" data-testid="live-detail">
          {detail}
        </span>
      </div>
      {state === "offline" ? (
        <Button type="button" variant="ghost" size="sm" onClick={retry} data-testid="live-retry">
          <RotateCw aria-hidden="true" />
          <span className="max-sm:sr-only">{t("retry")}</span>
        </Button>
      ) : state === "paused" ? (
        <Button type="button" variant="ghost" size="sm" onClick={resume} data-testid="live-resume">
          <Play aria-hidden="true" />
          <span className="max-sm:sr-only">{t("resume")}</span>
        </Button>
      ) : null}
    </div>
  );
}
