"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ExternalLink, type LucideIcon, MessagesSquare, ScrollText, TextQuote, TextSearch } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { DataCard, DataToolbar } from "@/components/data/data-card";
import { FacetGroup } from "@/components/data/facet-group";
import { Identifier } from "@/components/data/identifier";
import { SearchField } from "@/components/data/search-field";
import { projectHref } from "@/components/shell/nav";
import { ApiErrorState, NoResults } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { chatHref } from "@/lib/chat-keys";
import { cn } from "@/lib/utils";

import { type HistoryFilters, markedParts, searchQueryOf } from "./history-model";
import { HISTORY_SOURCES, type HistoryHit, type HistorySource, HITS_PAGE, searchQuery } from "./insight-queries";
import { useInsightFormat } from "./insight-format";
import { PANEL_LINK, PanelLine, PrevNext } from "./panel";
import { TranscriptExcerpt } from "./transcript-excerpt";

const SOURCE_ICON: Record<HistorySource, LucideIcon> = { transcript: TextQuote, run: ScrollText, chat: MessagesSquare };

/** A run's log, as a run's page opens it (`?view=log`); the runs folder is another feature's. */
function runLogHref(project: string, run: number): Route {
  return `${projectHref(project, `runs/${run}`)}?view=log` as Route;
}

/** The words around the match, the matched ones marked: `brand-soft` under them, so the eye finds them. */
function HitText({ text }: { text: string }) {
  return (
    <p className="text-[13px] leading-5 text-pretty text-foreground [overflow-wrap:anywhere]" data-testid="history-hit-text">
      {markedParts(text).map((part, index) =>
        part.marked ? (
          <mark key={index} className="rounded-xs bg-brand-soft px-0.5 font-medium text-foreground">
            {part.text}
          </mark>
        ) : (
          <span key={index}>{part.text}</span>
        ),
      )}
    </p>
  );
}

function Hit({ project, hit }: { project: string; hit: HistoryHit }) {
  const t = useTranslations("curator.history.search");
  const format = useInsightFormat();
  const ids = useId();
  const [open, setOpen] = useState(false);
  const Icon = SOURCE_ICON[hit.source];
  const transcript = hit.source === "transcript" && hit.session_id !== null && hit.line !== null;
  return (
    <li className="flex min-w-0 items-start gap-3 px-4 py-3" data-testid="history-hit" data-source={hit.source}>
      <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          <span className="font-medium text-foreground">{t(`kind.${hit.source}`)}</span>
          {hit.source === "chat" && hit.chat ? <span className="truncate">{hit.chat}</span> : null}
          {hit.run_id !== null ? <span className="font-mono tabular-nums">#{hit.run_id}</span> : null}
          {hit.source === "chat" ? null : <span className="font-mono">{hit.role}</span>}
          {hit.login ? <span className="font-mono">{hit.login}</span> : null}
          {hit.at ? (
            <time dateTime={hit.at} className="text-fg-subtle tabular-nums">
              {format.moment(hit.at)}
            </time>
          ) : null}
        </div>
        <HitText text={hit.text} />
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
          {transcript ? (
            <Button
              type="button"
              variant="link"
              size="sm"
              className="h-auto px-0 text-xs"
              aria-expanded={open}
              aria-controls={`${ids}-lines`}
              onClick={() => setOpen(!open)}
              data-testid="history-hit-lines"
            >
              <ChevronDown className={cn("transition-transform duration-base motion-reduce:transition-none", open && "rotate-180")} aria-hidden="true" />
              {open ? t("hideLines") : t("showLines")}
            </Button>
          ) : null}
          {hit.source === "run" && hit.run_id !== null ? (
            <Link href={runLogHref(project, hit.run_id)} className={cn(PANEL_LINK, "text-xs")} data-testid="history-hit-run">
              {t("openRun", { id: hit.run_id })}
            </Link>
          ) : null}
          {hit.source === "chat" && hit.chat_id !== null ? (
            <Link href={chatHref(hit.chat_id)} className={cn(PANEL_LINK, "inline-flex items-center gap-1 text-xs")} data-testid="history-hit-chat">
              {t("openChat")}
              <ExternalLink className="size-3.5" aria-hidden="true" />
            </Link>
          ) : null}
          {hit.evidence ? (
            <Identifier value={hit.evidence} copy copyLabel={t("copyEvidence", { evidence: hit.evidence })} title={hit.evidence} className="max-w-72" />
          ) : null}
        </div>
        {transcript ? (
          <div id={`${ids}-lines`} hidden={!open}>
            {open ? <TranscriptExcerpt project={project} session={hit.session_id as string} file={hit.file ?? "main"} line={hit.line as number} mode="around" testId="history-hit-excerpt" /> : null}
          </div>
        ) : null}
      </div>
    </li>
  );
}

function HitsSkeleton() {
  return (
    <ul className="flex flex-col divide-y" aria-hidden="true">
      {Array.from({ length: 3 }, (_, index) => (
        <li key={index} className="flex flex-col gap-2 px-4 py-3">
          <Skeleton className="h-3 w-1/3" />
          <Skeleton className="h-3 w-5/6" />
        </li>
      ))}
    </ul>
  );
}

/**
 * The search over the project's whole history: the words of every transcript the hub holds, every run log and the
 * group chats the visitor is in (each source alone, or all), best first, each hit with the words around the match
 * marked, who and when, the evidence a finding would cite, and where it leads: the lines of the transcript around it,
 * the run's log, the chat. The words and the source live in the URL.
 */
export function HistorySearchSection({ project, filters, onFilters }: { project: string; filters: HistoryFilters; onFilters: (next: HistoryFilters) => void }) {
  const t = useTranslations("curator.history.search");
  const query = searchQueryOf(filters);
  const found = useQuery(searchQuery(browserApi, project, query));
  const resultsId = useId();
  const source = filters.sources.length === 1 ? filters.sources[0] : null;
  const clear = () => onFilters({ ...filters, q: "", sources: [], hits: 1 });
  let body;
  if (!query.q) {
    body = <PanelLine testId="history-search-idle">{t("idle")}</PanelLine>;
  } else if (found.isPending) {
    body = <HitsSkeleton />;
  } else if (found.isError) {
    body = <ApiErrorState error={found.error.info} onRetry={() => void found.refetch()} />;
  } else if (found.data.hits.length === 0) {
    const used = [{ label: t("filterWords"), value: query.q }, ...(source ? [{ label: t("filterSource"), value: t(`source.${source}`) }] : [])];
    body = <NoResults title={t("none")} filters={used} onClear={clear} testId="history-search-none" />;
  } else {
    body = (
      <ul className="flex flex-col divide-y" aria-label={t("hits", { q: query.q })} data-testid="history-hits">
        {found.data.hits.map((hit, index) => (
          <Hit key={`${hit.source}-${hit.evidence ?? `${hit.chat_id}-${index}`}-${index}`} project={project} hit={hit} />
        ))}
      </ul>
    );
  }
  return (
    <section aria-labelledby="history-search-title" className="flex min-w-0 flex-col gap-3" data-testid="history-search">
      <div className="flex flex-col gap-0.5">
        <h2 id="history-search-title" className="text-[15px] leading-[22px] font-semibold">
          {t("title")}
        </h2>
        <p className="text-[13px] text-pretty text-muted-foreground">{t("description")}</p>
      </div>
      <DataCard
        busy={found.isPlaceholderData}
        toolbar={
          <DataToolbar
            label={t("label")}
            count={found.data && query.q ? t("total", { count: found.data.total }) : null}
            countTestId="history-search-total"
          >
            <SearchField
              value={filters.q}
              onCommit={(q) => onFilters({ ...filters, q, hits: 1 })}
              label={t("label")}
              placeholder={t("placeholder")}
              clearLabel={t("clear")}
              debounce={500}
              controls={resultsId}
              className="w-full sm:max-w-md"
              testId="history-search-field"
            />
            <FacetGroup
              label={t("sources")}
              options={[{ value: null, label: t("allSources"), icon: TextSearch }, ...HISTORY_SOURCES.map((value) => ({ value, label: t(`source.${value}`), icon: SOURCE_ICON[value] }))]}
              selected={source}
              onSelect={(value) => onFilters({ ...filters, sources: value ? [value as HistorySource] : [], hits: 1 })}
              countLabel={(count) => t("hitCount", { count })}
              testId="history-search-sources"
            />
          </DataToolbar>
        }
        footer={
          found.data && query.q ? (
            <PrevNext
              label={t("title")}
              page={filters.hits}
              perPage={HITS_PAGE}
              shown={found.data.hits.length}
              total={found.data.total}
              busy={found.isPlaceholderData}
              onPage={(hits) => onFilters({ ...filters, hits })}
              testId="history-search-pages"
            />
          ) : null
        }
      >
        <div id={resultsId}>{body}</div>
      </DataCard>
    </section>
  );
}
