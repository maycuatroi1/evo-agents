"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import { useFormatter, useTranslations } from "next-intl";
import { memo, type RefObject, useLayoutEffect } from "react";

import { cn } from "@/lib/utils";

import { type LogLine, splitMatches } from "./log-model";
import type { RunEventKind } from "./queries";

/** The rows of a run's Raw log (run-log.tsx): every line drawn, or past VIRTUAL_THRESHOLD only the rows in view. */

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

export function PlainRows({ lines, query }: { lines: readonly LogLine[]; query: string }) {
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

export type Virtual = ReturnType<typeof useVirtualizer<HTMLDivElement, HTMLLIElement>>;

export function VirtualRows({
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
