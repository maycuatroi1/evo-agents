"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronUp } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Identifier } from "@/components/data/identifier";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import { transcriptWindowQuery } from "./insight-queries";

/** Lines read before and after the line asked for around a hit, and from a session's first line. */
const AROUND = 8;
const FROM = 40;

function roleTone(role: string): string {
  if (/\b(failed|error)\b/.test(role)) return "text-danger";
  if (role === "user") return "text-foreground";
  return "text-muted-foreground";
}

/**
 * A stretch of a transcript the hub holds, read when asked for: the lines around a search hit (`around`), or the
 * first lines of a session (`from`), each line as its entries (what the person wrote, what the agent said, each tool
 * call and its result), the line asked for marked. Earlier lines and Later lines move the stretch within the file; the
 * evidence a finding would cite for the line asked for copies whole. A transcript the hub no longer holds, or one
 * beyond the visitor's grant, says so.
 */
export function TranscriptExcerpt({
  project,
  session,
  file = "main",
  line,
  mode,
  testId = "transcript-excerpt",
}: {
  project: string;
  session: string;
  file?: string;
  line: number;
  mode: "around" | "from";
  testId?: string;
}) {
  const t = useTranslations("curator.history.excerpt");
  const [center, setCenter] = useState(line);
  const before = mode === "around" ? AROUND : 0;
  const after = mode === "around" ? AROUND : FROM;
  const lines = useQuery(transcriptWindowQuery(browserApi, project, session, file, center, before, after));
  if (lines.isPending) {
    return (
      <div role="status" aria-label={t("loading")} className="flex flex-col gap-1.5 rounded-sm border bg-surface-sunken p-3" data-testid={`${testId}-loading`}>
        <Skeleton className="h-3 w-48" />
        <Skeleton className="h-16 w-full" />
      </div>
    );
  }
  if (lines.isError) {
    return (
      <p className="rounded-sm border bg-surface-sunken px-3 py-2.5 text-xs text-danger" role="alert" data-testid={`${testId}-error`}>
        {lines.error.status === 404 ? t("gone") : t("failed")}{" "}
        {lines.error.status === 404 ? null : (
          <Button variant="link" size="sm" className="h-auto px-0 text-xs" onClick={() => void lines.refetch()}>
            {t("retry")}
          </Button>
        )}
      </p>
    );
  }
  const found = lines.data;
  const atStart = found.first <= 1;
  const atEnd = found.last >= found.lines;
  // The next stretch starts right after the last line shown, the one before ends right before the first.
  const clamp = (value: number) => Math.min(Math.max(1, value), Math.max(1, found.lines));
  const earlier = () => setCenter(clamp(found.first - 1 - after));
  const later = () => setCenter(clamp(found.last + 1 + before));
  return (
    <div className="flex min-w-0 flex-col gap-2 rounded-sm border bg-surface-sunken p-3" data-testid={testId} data-first={found.first} data-last={found.last}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <span aria-live="polite" aria-atomic="true" data-testid={`${testId}-span`}>
          {t("span", { first: found.first, last: found.last, lines: found.lines, file: found.file })}
        </span>
        <span className="font-mono">{found.login}</span>
        {mode === "from" || center !== line ? (
          <Identifier value={found.evidence} copy copyLabel={t("copyEvidence", { evidence: found.evidence })} title={found.evidence} className="max-w-64">
            {found.evidence}
          </Identifier>
        ) : null}
      </div>
      {atStart ? null : (
        <Button type="button" variant="ghost" size="sm" className="w-fit" onClick={earlier} data-testid={`${testId}-earlier`}>
          <ChevronUp aria-hidden="true" />
          {t("earlier")}
        </Button>
      )}
      {found.entries.length === 0 ? (
        <p className="text-xs text-muted-foreground">{t("empty")}</p>
      ) : (
        <ol className="flex max-h-96 min-w-0 flex-col overflow-y-auto rounded-xs border bg-card font-mono text-xs leading-5" aria-label={t("label", { session })} tabIndex={0}>
          {found.entries.map((entry, index) => {
            const asked = mode === "around" && entry.line === line;
            return (
              <li
                key={`${entry.line}-${index}`}
                className={cn("grid grid-cols-[3.5rem_minmax(0,1fr)] gap-x-2 border-b px-2 py-1 last:border-b-0", asked && "bg-surface-selected")}
                data-line={entry.line}
                data-asked={asked || undefined}
              >
                <span className="text-right text-fg-subtle tabular-nums">
                  {asked ? <span className="sr-only">{t("asked")} </span> : null}L{entry.line}
                </span>
                <span className="min-w-0 [overflow-wrap:anywhere] whitespace-pre-wrap">
                  <span className={cn("font-medium", roleTone(entry.role))}>{entry.role}</span>
                  <span className="text-foreground">: {entry.text}</span>
                </span>
              </li>
            );
          })}
        </ol>
      )}
      {atEnd ? null : (
        <Button type="button" variant="ghost" size="sm" className="w-fit" onClick={later} data-testid={`${testId}-later`}>
          <ChevronDown aria-hidden="true" />
          {t("later")}
        </Button>
      )}
    </div>
  );
}
