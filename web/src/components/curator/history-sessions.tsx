"use client";

import { useQuery } from "@tanstack/react-query";
import { Boxes, ChevronDown, CircleCheck, CircleDashed, CircleHelp, CircleMinus, CircleX, type LucideIcon, MessagesSquare, ScrollText, X } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { DataCard, DataToolbar } from "@/components/data/data-card";
import { FacetGroup } from "@/components/data/facet-group";
import { Identifier, Tag } from "@/components/data/identifier";
import { projectHref } from "@/components/shell/nav";
import { ApiErrorState, NoResults } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import { evidenceRef, facetNumbers, type HistoryFilters, type Outcome, OUTCOMES, sessionsQueryOf } from "./history-model";
import { type FacetCluster, type HistorySessionItem, SESSIONS_PAGE, sessionsQuery } from "./insight-queries";
import { useInsightFormat } from "./insight-format";
import { PANEL_LINK, PanelLine, PanelSkeleton, PrevNext } from "./panel";
import { TranscriptExcerpt } from "./transcript-excerpt";

const OUTCOME_ICON: Record<Outcome, LucideIcon> = {
  achieved: CircleCheck,
  partial: CircleDashed,
  failed: CircleX,
  abandoned: CircleMinus,
  unclear: CircleHelp,
};

function runHref(project: string, run: number, log = false): Route {
  return `${projectHref(project, `runs/${run}`)}${log ? "?view=log" : ""}` as Route;
}

/** A failure mode of MAST by its code, as messages key it (a dot would split the key). */
export function useModeText() {
  const t = useTranslations("curator.history.mast");
  return (code: string) => {
    const key = code.replace(".", "_");
    return t.has(key as never) ? t(key as never) : code;
  };
}

export function OutcomeTag({ outcome }: { outcome: string | null }) {
  const t = useTranslations("curator.history.outcome");
  if (!outcome || !(OUTCOMES as readonly string[]).includes(outcome)) return null;
  const Icon = OUTCOME_ICON[outcome as Outcome];
  return (
    <Tag className={cn(outcome === "failed" && "bg-danger-soft text-danger", outcome === "achieved" && "bg-success-soft text-success")} data-testid="history-outcome" data-outcome={outcome}>
      <Icon aria-hidden="true" />
      {t(outcome as Outcome)}
    </Tag>
  );
}

/** What a piece of evidence opens: the first lines of a session's transcript, read when asked, or a run's log. */
export function EvidenceOpener({ project, evidence, held = true, testId }: { project: string; evidence: string; held?: boolean; testId: string }) {
  const t = useTranslations("curator.history.sessions");
  const ids = useId();
  const [open, setOpen] = useState(false);
  const ref = evidenceRef(evidence);
  if (ref === null) return null;
  if (ref.kind === "run") {
    return (
      <Link href={runHref(project, ref.run, true)} className={cn(PANEL_LINK, "text-xs")} data-testid={`${testId}-run`}>
        {t("openRun", { id: ref.run })}
      </Link>
    );
  }
  if (!held) return null;
  const line = ref.kind === "transcript" ? ref.line : 1;
  const file = ref.kind === "transcript" ? ref.file : "main";
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <Button
        type="button"
        variant="link"
        size="sm"
        className="h-auto w-fit px-0 text-xs"
        aria-expanded={open}
        aria-controls={`${ids}-transcript`}
        onClick={() => setOpen(!open)}
        data-testid={`${testId}-toggle`}
      >
        <ChevronDown className={cn("transition-transform duration-base motion-reduce:transition-none", open && "rotate-180")} aria-hidden="true" />
        {open ? t("hideTranscript") : t("readTranscript")}
      </Button>
      <div id={`${ids}-transcript`} hidden={!open}>
        {open ? <TranscriptExcerpt project={project} session={ref.session} file={file} line={line} mode={ref.kind === "transcript" ? "around" : "from"} testId={`${testId}-excerpt`} /> : null}
      </div>
    </div>
  );
}

function SessionItem({ project, item }: { project: string; item: HistorySessionItem }) {
  const t = useTranslations("curator.history.sessions");
  const mode = useModeText();
  const format = useInsightFormat();
  const numbers = facetNumbers(item.numbers as Record<string, unknown>);
  const facts = [
    t("messages", { count: numbers.messages }),
    t("toolErrors", { count: numbers.toolErrors }),
    ...(numbers.corrections > 0 ? [t("corrections", { count: numbers.corrections })] : []),
    ...(numbers.cost !== null ? [format.money(numbers.cost)] : []),
  ];
  return (
    <li className="flex min-w-0 items-start gap-3 px-4 py-3" data-testid="history-session" data-kind={item.kind} data-evidence={item.evidence}>
      {item.kind === "run" ? (
        <ScrollText className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      ) : (
        <MessagesSquare className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      )}
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          {item.kind === "run" && item.run_id !== null ? (
            <Link href={runHref(project, item.run_id)} className="font-mono text-[13px] font-medium text-foreground tabular-nums hover:text-brand" aria-label={t("openRunPage", { id: item.run_id })}>
              #{item.run_id}
            </Link>
          ) : item.session_id ? (
            <Identifier value={item.session_id} title={item.session_id} className="max-w-56">
              {item.session_id.slice(0, 13)}
            </Identifier>
          ) : null}
          {item.run_kind ? <Tag>{item.run_kind}</Tag> : null}
          {item.login ? <span className="font-mono">{item.login}</span> : null}
          {item.runtime ? <span>{item.runtime}</span> : null}
          {item.at ? (
            <time dateTime={item.at} className="text-fg-subtle tabular-nums">
              {format.moment(item.at)}
            </time>
          ) : null}
        </div>
        {item.summary ? (
          <p className="text-[13px] leading-5 text-pretty text-foreground [overflow-wrap:anywhere]" data-testid="history-session-summary">
            {item.summary}
          </p>
        ) : (
          <p className="text-[13px] text-fg-subtle">{t("notRead")}</p>
        )}
        <div className="flex min-w-0 flex-wrap items-center gap-1.5">
          <OutcomeTag outcome={item.outcome} />
          {item.failure_modes.map((code) => (
            <Tag key={code} title={mode(code)} data-testid="history-mode">
              <span className="font-mono">{code}</span>
              <span className="sr-only">{mode(code)}</span>
            </Tag>
          ))}
          <span className="text-xs text-fg-subtle tabular-nums">{facts.join(", ")}</span>
        </div>
        <EvidenceOpener project={project} evidence={item.evidence} held={item.transcript} testId="history-session-open" />
      </div>
    </li>
  );
}

/**
 * The sessions and runs of the project's whole history, the latest first, each with what the cheap model wrote of
 * it (its summary, outcome and failure modes), the numbers of its facet, and its transcript or its run's log, read
 * when asked. Filtered by kind, outcome and cluster, all in the URL.
 */
export function HistorySessionsSection({
  project,
  filters,
  onFilters,
  clusters,
}: {
  project: string;
  filters: HistoryFilters;
  onFilters: (next: HistoryFilters) => void;
  clusters: readonly FacetCluster[];
}) {
  const t = useTranslations("curator.history.sessions");
  const tOutcome = useTranslations("curator.history.outcome");
  const ids = useId();
  const found = useQuery(sessionsQuery(browserApi, project, sessionsQueryOf(filters)));
  const cluster = filters.cluster ? (clusters.find((item) => item.key === filters.cluster || item.keys.includes(filters.cluster ?? ""))?.name ?? filters.cluster) : null;
  const filtered = filters.kind !== null || filters.outcome !== null || filters.cluster !== null;
  const reset = { ...filters, kind: null, outcome: null, cluster: null, page: 1 };
  let body;
  if (found.isPending) {
    body = <PanelSkeleton rows={4} />;
  } else if (found.isError) {
    body = <ApiErrorState error={found.error.info} onRetry={() => void found.refetch()} />;
  } else if (found.data.items.length === 0) {
    body = filtered ? (
      <NoResults
        title={t("none")}
        filters={[
          ...(filters.kind ? [{ label: t("kind"), value: t(`kinds.${filters.kind}`) }] : []),
          ...(filters.outcome ? [{ label: t("outcome"), value: tOutcome(filters.outcome) }] : []),
          ...(cluster ? [{ label: t("cluster"), value: cluster }] : []),
        ]}
        onClear={() => onFilters(reset)}
        testId="history-sessions-none"
      />
    ) : (
      <PanelLine testId="history-sessions-empty">{t("empty")}</PanelLine>
    );
  } else {
    body = (
      <ul className="flex flex-col divide-y" aria-label={t("title")} data-testid="history-sessions-list">
        {found.data.items.map((item) => (
          <SessionItem key={item.evidence} project={project} item={item} />
        ))}
      </ul>
    );
  }
  return (
    <section aria-labelledby="history-sessions-title" className="flex min-w-0 flex-col gap-3" data-testid="history-sessions">
      <div className="flex flex-col gap-0.5">
        <h2 id="history-sessions-title" className="text-[15px] leading-[22px] font-semibold">
          {t("title")}
        </h2>
        <p className="text-[13px] text-pretty text-muted-foreground">{t("description")}</p>
      </div>
      <DataCard
        busy={found.isPlaceholderData}
        toolbar={
          <DataToolbar label={t("label")} count={found.data ? t("total", { count: found.data.total }) : null} countTestId="history-sessions-total">
            <FacetGroup
              label={t("kind")}
              options={[
                { value: null, label: t("kinds.all") },
                { value: "session", label: t("kinds.session"), icon: MessagesSquare },
                { value: "run", label: t("kinds.run"), icon: ScrollText },
              ]}
              selected={filters.kind}
              onSelect={(value) => onFilters({ ...filters, kind: value === "session" || value === "run" ? value : null, page: 1 })}
              countLabel={(count) => t("total", { count })}
              testId="history-sessions-kind"
            />
            <label htmlFor={`${ids}-outcome`} className="sr-only">
              {t("outcome")}
            </label>
            <NativeSelect
              id={`${ids}-outcome`}
              value={filters.outcome ?? ""}
              onChange={(event) => {
                const value = event.target.value;
                onFilters({ ...filters, outcome: (OUTCOMES as readonly string[]).includes(value) ? (value as Outcome) : null, page: 1 });
              }}
              data-testid="history-sessions-outcome"
            >
              <NativeSelectOption value="">{t("allOutcomes")}</NativeSelectOption>
              {OUTCOMES.map((value) => (
                <NativeSelectOption key={value} value={value}>
                  {tOutcome(value)}
                </NativeSelectOption>
              ))}
            </NativeSelect>
            {cluster ? (
              <Button
                type="button"
                variant="secondary"
                size="sm"
                className="border-brand bg-surface-selected font-semibold text-foreground"
                onClick={() => onFilters({ ...filters, cluster: null, page: 1 })}
                aria-label={t("removeCluster", { name: cluster })}
                data-testid="history-sessions-cluster"
              >
                <Boxes aria-hidden="true" />
                <span className="max-w-48 truncate">{cluster}</span>
                <X aria-hidden="true" />
              </Button>
            ) : null}
          </DataToolbar>
        }
        footer={
          found.data ? (
            <PrevNext
              label={t("title")}
              page={filters.page}
              perPage={SESSIONS_PAGE}
              shown={found.data.items.length}
              total={found.data.total}
              busy={found.isPlaceholderData}
              onPage={(page) => onFilters({ ...filters, page })}
              testId="history-sessions-pages"
            />
          ) : null
        }
      >
        {body}
      </DataCard>
    </section>
  );
}
