"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import type { TraceSummary } from "@/lib/systems";
import { withTrace } from "@/lib/systems-observe";
import { systemKeys, systemTraceStreamPath, TRACE_LIST_LIMIT } from "@/lib/systems-queries";

/**
 * The traces of a system as they move (`GET .../traces/stream`, docs/hub.md, reading a trace), followed while the tab
 * is in view: each `trace` event puts the trace's summary in the list of recent traces and reads again what depends on
 * it (that trace, the metrics). A hidden tab closes the stream; showing it again reads the list and the metrics and
 * opens the stream from the last time it saw, so nothing that moved meanwhile is missed. The status says only whether
 * the stream fails, for a page that shows nothing while all is well.
 */
export type TraceStreamStatus = "live" | "paused" | "failing" | "ended";

const CLOSED = 2; // EventSource.CLOSED: the browser gave up and will not reconnect
const RETRY_MS = 15_000;

function parse(data: string): TraceSummary | null {
  try {
    const body = JSON.parse(data) as TraceSummary;
    return typeof body?.id === "number" ? body : null;
  } catch {
    return null;
  }
}

export function useTraceStream(project: string, slug: string, onTrace?: (trace: TraceSummary) => void): TraceStreamStatus {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<TraceStreamStatus>("live");
  const handler = useRef(onTrace);
  useEffect(() => {
    handler.current = onTrace;
  });

  useEffect(() => {
    let source: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let seen: string | null = null;
    let ended = false;

    const close = () => {
      if (retry) clearTimeout(retry);
      retry = null;
      if (!source) return;
      source.onerror = null;
      source.close();
      source = null;
    };
    const refresh = () => {
      // The list and every trace under it (their keys start with the list's), then the metrics.
      void queryClient.invalidateQueries({ queryKey: systemKeys.traces(project, slug) });
      void queryClient.invalidateQueries({ queryKey: systemKeys.metrics(project, slug) });
    };
    const open = () => {
      close();
      if (ended || document.visibilityState !== "visible") return;
      const opened = new EventSource(systemTraceStreamPath(project, slug, seen));
      source = opened;
      opened.onopen = () => setStatus("live");
      opened.addEventListener("trace", (message) => {
        const trace = parse((message as MessageEvent<string>).data);
        if (!trace || source !== opened) return;
        if (seen === null || trace.last_at > seen) seen = trace.last_at;
        queryClient.setQueryData<TraceSummary[]>(systemKeys.traces(project, slug), (list) => withTrace(list, trace, TRACE_LIST_LIMIT));
        void queryClient.invalidateQueries({ queryKey: systemKeys.trace(project, slug, trace.id) });
        void queryClient.invalidateQueries({ queryKey: systemKeys.metrics(project, slug) });
        handler.current?.(trace);
      });
      opened.addEventListener("end", () => {
        if (source !== opened) return;
        ended = true;
        close();
        setStatus("ended");
      });
      opened.onerror = () => {
        if (source !== opened || opened.readyState !== CLOSED) return;
        close();
        setStatus("failing");
        retry = setTimeout(open, RETRY_MS);
      };
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        setStatus((current) => (current === "paused" ? "live" : current));
        refresh();
        open();
      } else {
        close();
        setStatus((current) => (current === "ended" ? current : "paused"));
      }
    };

    open();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      close();
    };
  }, [project, slug, queryClient]);

  return status;
}
