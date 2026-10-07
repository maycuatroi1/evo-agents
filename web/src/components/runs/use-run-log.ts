"use client";

import { useEffect, useRef, useState } from "react";

import type { LiveSignal } from "@/components/live/live-model";
import { browserApi } from "@/lib/api/browser";
import { type ApiErrorInfo, isApiError, toInfo } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";

import { type DescribeMove, type LogLine, moveOf, type RunMove, toLogLine } from "./log-model";
import { isTerminalState, type RunEvent, runEvents, type RunState, runStreamPath } from "./queries";

/**
 * A run's log in the browser, live.
 *
 * The page follows `GET .../runs/{id}/stream` with an EventSource: each event's id is its seq, so when the connection
 * drops the browser reconnects by itself with `Last-Event-ID` and the hub goes on after the last event the page got.
 * Every event is also checked against the last seq kept, so none is ever shown twice. Once the run is final and every
 * event was sent, the stream says `end`; the page closes it then and never reconnects.
 *
 * When the stream fails for good (the browser gives up, it errs three times without opening, or it stays behind the
 * run's `last_seq` for 10 seconds, as behind a proxy that buffers it), the page reads `events?after=` every 1.5 seconds
 * instead and tries the stream again every 30 seconds; a stream that was behind is not tried again. Events arriving
 * together are added to the log in one render.
 */

export type LogStatus = "connecting" | "live" | "reconnecting" | "polling" | "ended" | "failed";

/** Errors in a row, without the stream opening between them, before the page reads events instead. */
export const MAX_STREAM_ERRORS = 3;
export const POLL_MS = 1_500;
export const STREAM_RETRY_MS = 30_000;
/** How long the stream may stay behind the run's last_seq before the page reads events instead. */
export const STALL_MS = 10_000;
const WATCH_MS = 2_000;
/** `EventSource.CLOSED`: the browser gave up and will not reconnect. */
const CLOSED = 2;
const FLUSH_MS = 50;
const MAX_POLL_BACKOFF_MS = 15_000;

export type RunLog = {
  lines: LogLine[];
  moves: RunMove[];
  status: LogStatus;
  /** The final state the stream (or a read of the events) reported; null while the run goes on. */
  endState: RunState | null;
  /** The seq of the latest event shown. */
  lastSeq: number;
  /** Why the log could not be read at all (403 or 404 while reading events). */
  failure: ApiErrorInfo | null;
  /** When the log last heard from the hub (ms since the epoch): events, the stream opening, or a read of events. */
  updatedAt: number;
  /** While the page reads events instead: when it tries the stream again (ms since the epoch), null if it will not. */
  streamRetryAt: number | null;
  /** The last read of events failed (no answer, or a 5xx); the page tries again, waiting longer each time. */
  pollFailing: boolean;
};

type Options = {
  project: string;
  runId: number;
  /** The run's last_seq as its query last read it. */
  knownLastSeq: number;
  describe: DescribeMove;
  /** For tests: what opens the stream. */
  openSource?: (url: string) => EventSource;
};

function parseEvent(data: string): RunEvent | null {
  try {
    const value = JSON.parse(data) as RunEvent;
    return typeof value?.seq === "number" && typeof value.kind === "string" ? value : null;
  } catch {
    return null;
  }
}

function parseEnd(data: string): { state: RunState | null; last_seq: number | null } {
  try {
    const value = JSON.parse(data) as { state?: unknown; last_seq?: unknown };
    return {
      state: typeof value.state === "string" ? (value.state as RunState) : null,
      last_seq: typeof value.last_seq === "number" ? value.last_seq : null,
    };
  } catch {
    return { state: null, last_seq: null };
  }
}

const defaultSource = (url: string) => new EventSource(url);

export function useRunLog({ project, runId, knownLastSeq, describe, openSource = defaultSource }: Options): RunLog {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [moves, setMoves] = useState<RunMove[]>([]);
  const [status, setStatus] = useState<LogStatus>("connecting");
  const [endState, setEndState] = useState<RunState | null>(null);
  const [failure, setFailure] = useState<ApiErrorInfo | null>(null);
  const [shownSeq, setShownSeq] = useState(0);
  const [updatedAt, setUpdatedAt] = useState(0);
  const [streamRetryAt, setStreamRetryAt] = useState<number | null>(null);
  const [pollFailing, setPollFailing] = useState(false);

  const describeRef = useRef(describe);
  const knownRef = useRef(knownLastSeq);
  const sourceRef = useRef(openSource);
  useEffect(() => {
    describeRef.current = describe;
    knownRef.current = knownLastSeq;
    sourceRef.current = openSource;
  });

  useEffect(() => {
    let disposed = false;
    let lastSeq = 0;
    let source: EventSource | null = null;
    let streamUsable = true;
    let errorsSinceOpen = 0;
    let streaming = false; // the stream, not the reads of events, is what the page follows now
    let polling = false;
    let pollTimer: ReturnType<typeof setTimeout> | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let flushTimer: ReturnType<typeof setTimeout> | null = null;
    let pollAbort: AbortController | null = null;
    let pollFailures = 0;
    let behindSince: number | null = null;
    let pending: RunEvent[] = [];

    const flush = () => {
      flushTimer = null;
      if (disposed || pending.length === 0) return;
      const batch = pending;
      pending = [];
      const describeMove = describeRef.current;
      const added = batch.map((event) => toLogLine(event, describeMove));
      const moved = batch.map(moveOf).filter((move): move is RunMove => move !== null);
      setLines((previous) => previous.concat(added));
      if (moved.length) setMoves((previous) => previous.concat(moved));
      setShownSeq(lastSeq);
      setUpdatedAt(Date.now());
    };

    /** Keep an event once: anything at or below the last seq kept is a repeat. */
    const receive = (event: RunEvent) => {
      if (event.seq <= lastSeq) return;
      lastSeq = event.seq;
      pending.push(event);
      flushTimer ??= setTimeout(flush, FLUSH_MS);
    };

    const closeStream = () => {
      streaming = false;
      if (source) {
        source.onopen = null;
        source.onmessage = null;
        source.onerror = null;
        source.close();
        source = null;
      }
    };

    const stopPolling = () => {
      polling = false;
      if (pollTimer) clearTimeout(pollTimer);
      pollTimer = null;
      if (retryTimer) clearTimeout(retryTimer);
      retryTimer = null;
      pollAbort?.abort();
      pollAbort = null;
      setStreamRetryAt(null);
      setPollFailing(false);
    };

    const finish = (state: RunState | null) => {
      closeStream();
      stopPolling();
      flush();
      setEndState(state);
      setStatus("ended");
    };

    const schedulePoll = (delay: number) => {
      if (disposed || !polling) return;
      if (pollTimer) clearTimeout(pollTimer);
      pollTimer = setTimeout(() => void poll(), delay);
    };

    const poll = async () => {
      pollTimer = null;
      const controller = new AbortController();
      pollAbort = controller;
      try {
        const page = await runEvents(browserApi(), project, runId, lastSeq, controller.signal);
        if (disposed || controller.signal.aborted) return;
        pollFailures = 0;
        setPollFailing(false);
        setUpdatedAt(Date.now());
        for (const event of page.events) receive(event);
        if (page.more) return schedulePoll(0);
        if (isTerminalState(page.state) && lastSeq >= page.last_seq) return finish(page.state);
        schedulePoll(POLL_MS);
      } catch (error) {
        if (disposed || controller.signal.aborted) return;
        if (isApiError(error) && error.kind === "unauthorized") {
          window.location.assign(LOGIN_PATH);
          return;
        }
        if (isApiError(error) && (error.kind === "forbidden" || error.kind === "not_found")) {
          stopPolling();
          setFailure(toInfo(error));
          setStatus("failed");
          return;
        }
        pollFailures += 1; // no answer, or a 5xx: try again, waiting longer each time
        setPollFailing(true);
        schedulePoll(Math.min(POLL_MS * 2 ** pollFailures, MAX_POLL_BACKOFF_MS));
      }
    };

    const scheduleStreamRetry = () => {
      if (retryTimer) clearTimeout(retryTimer);
      retryTimer = null;
      if (!streamUsable) {
        setStreamRetryAt(null);
        return;
      }
      setStreamRetryAt(Date.now() + STREAM_RETRY_MS);
      retryTimer = setTimeout(() => {
        retryTimer = null;
        if (!disposed && polling) openStream();
      }, STREAM_RETRY_MS);
    };

    const startPolling = () => {
      if (disposed) return;
      closeStream();
      if (!polling) {
        polling = true;
        setStatus("polling");
        schedulePoll(0);
      }
      scheduleStreamRetry();
    };

    function openStream() {
      if (disposed) return;
      closeStream();
      const opened = sourceRef.current(runStreamPath(project, runId, lastSeq));
      source = opened;
      streaming = true;
      errorsSinceOpen = 0;
      behindSince = null;
      // The status stays what it was (connecting at first, polling while a retry is tried) until the stream opens.
      opened.onopen = () => {
        if (disposed || source !== opened) return;
        errorsSinceOpen = 0;
        behindSince = null;
        stopPolling();
        setUpdatedAt(Date.now());
        setStatus("live");
      };
      opened.onmessage = (message: MessageEvent<string>) => {
        const event = parseEvent(message.data);
        if (event) receive(event);
      };
      opened.addEventListener("end", (message) => {
        if (disposed || source !== opened) return;
        finish(parseEnd((message as MessageEvent<string>).data).state);
      });
      opened.onerror = () => {
        if (disposed || source !== opened) return;
        errorsSinceOpen += 1;
        if (opened.readyState === CLOSED || errorsSinceOpen >= MAX_STREAM_ERRORS) {
          startPolling();
        } else if (!polling) {
          setStatus("reconnecting"); // the browser reconnects by itself, with Last-Event-ID
        }
      };
    }

    // A stream that stays behind what the hub says it holds is held up somewhere (a buffering proxy): read instead.
    const watch = setInterval(() => {
      if (disposed || !streaming || polling) return;
      if (knownRef.current > lastSeq) {
        behindSince ??= Date.now();
        if (Date.now() - behindSince >= STALL_MS) {
          streamUsable = false;
          startPolling();
        }
      } else {
        behindSince = null;
      }
    }, WATCH_MS);

    openStream();

    return () => {
      disposed = true;
      clearInterval(watch);
      if (flushTimer) clearTimeout(flushTimer);
      closeStream();
      stopPolling();
    };
  }, [project, runId]);

  return { lines, moves, status, endState, lastSeq: shownSeq, failure, updatedAt, streamRetryAt, pollFailing };
}

/**
 * The log as a source of the top bar's LiveIndicator: Live while the stream is open; Reconnecting while the browser
 * reconnects it (failing, so Offline after a while) or while the page reads events instead and will try the stream
 * again (degraded: updates still arrive); Offline-bound when those reads fail too; Paused while the person holds the
 * log. A stream that cannot be used at all leaves the reads as the transport, which is Live while they succeed. Null
 * before the stream opens and once it ended: the run's own query speaks for the page then.
 */
export function logSignal(log: Pick<RunLog, "status" | "updatedAt" | "streamRetryAt" | "pollFailing">, paused: boolean): LiveSignal | null {
  const base = { transport: "stream" as const, updatedAt: log.updatedAt, offline: false, paused, retryAt: null, pollMs: null };
  switch (log.status) {
    case "live":
      return { ...base, failing: false, degraded: false };
    case "reconnecting":
      return { ...base, failing: true, degraded: false };
    case "polling":
      if (log.pollFailing) return { ...base, failing: true, degraded: false, pollMs: POLL_MS };
      if (log.streamRetryAt === null) return { ...base, transport: "poll", failing: false, degraded: false, pollMs: POLL_MS };
      return { ...base, failing: false, degraded: true, retryAt: log.streamRetryAt, pollMs: POLL_MS };
    default:
      return null;
  }
}
