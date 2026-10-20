"use client";

import { ArrowRight } from "lucide-react";
import { useTranslations } from "next-intl";

import { Identifier } from "@/components/data/identifier";
import { cn } from "@/lib/utils";

import { type Retrieval, shortCommit, type SourceHealth, sourcesOf, type SyncState } from "./graph-health-model";
import { EvidenceOpener } from "./history-sessions";
import type { KgMeasure } from "./insight-queries";
import { useInsightFormat } from "./insight-format";
import { Fact, Facts } from "./parts";
import { NUMBER, Panel, PanelLine, PanelTable, TABLE_CELL, TABLE_HEAD } from "./panel";

function Commit({ sha, label }: { sha: string | null; label: string }) {
  if (!sha) return <span className="text-fg-subtle">-</span>;
  return (
    <Identifier value={sha} title={sha} copy copyLabel={label}>
      {shortCommit(sha)}
    </Identifier>
  );
}

/** The files a source is off by, a few of each kind, behind a native disclosure. */
function Samples({ source }: { source: SourceHealth }) {
  const t = useTranslations("curator.graph.sources");
  const kinds = (["missing", "stale", "deleted"] as const).filter((kind) => source.samples[kind].length > 0);
  if (kinds.length === 0) return null;
  return (
    <details className="group text-xs" data-testid="graph-source-samples">
      <summary className="w-fit cursor-pointer rounded-xs text-brand underline decoration-brand/40 underline-offset-4 hover:decoration-current max-md:py-3">
        {t("samples")}
      </summary>
      <dl className="mt-1.5 flex flex-col gap-1.5">
        {kinds.map((kind) => (
          <div key={kind} className="flex min-w-0 flex-col gap-0.5">
            <dt className="text-muted-foreground">{t(kind)}</dt>
            {source.samples[kind].map((path) => (
              <dd key={path} className="font-mono [overflow-wrap:anywhere] text-foreground">
                {path}
              </dd>
            ))}
          </div>
        ))}
      </dl>
    </details>
  );
}

/**
 * Each git source of the latest good measure against its repo's default branch: the commit the graph read and the
 * branch's head, the commits between them and how long the oldest waited, and the files the graph lacks, holds stale
 * or still holds after their deletion, with a few of each; why, for a part the hub could not read.
 */
export function GraphSources({ measure }: { measure: KgMeasure | null }) {
  const t = useTranslations("curator.graph.sources");
  const format = useInsightFormat();
  const sources = sourcesOf(measure);
  const count = (value: number | null) => (value === null ? <span className="text-fg-subtle">-</span> : format.count(value));
  return (
    <Panel title={t("title")} sub={t("sub")} testId="graph-sources">
      {sources.length === 0 ? (
        <PanelLine testId="graph-sources-empty">{t("empty")}</PanelLine>
      ) : (
        <PanelTable label={t("caption")} testId="graph-sources-table">
          <thead className="border-b bg-surface-sunken">
            <tr>
              <th scope="col" className={TABLE_HEAD}>
                {t("source")}
              </th>
              <th scope="col" className={TABLE_HEAD}>
                {t("commits")}
              </th>
              <th scope="col" className={cn(TABLE_HEAD, "text-right")}>
                {t("behind")}
              </th>
              <th scope="col" className={cn(TABLE_HEAD, "text-right")}>
                {t("missing")}
              </th>
              <th scope="col" className={cn(TABLE_HEAD, "text-right")}>
                {t("stale")}
              </th>
              <th scope="col" className={cn(TABLE_HEAD, "text-right")}>
                {t("deleted")}
              </th>
            </tr>
          </thead>
          {sources.map((source) => (
            <tbody key={source.source} className="border-b last:border-b-0" data-testid="graph-source" data-source={source.source}>
              <tr className="align-top">
                <th scope="row" className={cn(TABLE_CELL, "py-2.5 text-left font-normal")}>
                  <span className="block font-mono text-xs text-foreground">{source.source}</span>
                  {source.repo ? <span className="block text-xs text-fg-subtle">{source.repo}</span> : null}
                </th>
                <td className={cn(TABLE_CELL, "py-2.5")}>
                  <span className="inline-flex items-center gap-1.5">
                    <Commit sha={source.commit} label={t("copyRead", { source: source.source })} />
                    <ArrowRight className="size-3.5 shrink-0" aria-hidden="true" />
                    <span className="sr-only">{t("then", { branch: source.branch ?? "-" })}</span>
                    <Commit sha={source.head} label={t("copyHead", { source: source.source })} />
                  </span>
                  {source.branch ? <span className="mt-0.5 block text-xs text-fg-subtle">{source.branch}</span> : null}
                </td>
                <td className={cn(TABLE_CELL, NUMBER, "py-2.5")}>
                  {count(source.behind)}
                  {source.lagHours ? <span className="block text-xs text-fg-subtle">{t("waited", { hours: format.hours(source.lagHours) })}</span> : null}
                </td>
                <td className={cn(TABLE_CELL, NUMBER, "py-2.5")}>{count(source.missing)}</td>
                <td className={cn(TABLE_CELL, NUMBER, "py-2.5")}>{count(source.stale)}</td>
                <td className={cn(TABLE_CELL, NUMBER, "py-2.5")}>{count(source.deleted)}</td>
              </tr>
              {source.why || Object.values(source.samples).some((paths) => paths.length > 0) ? (
                <tr>
                  <td colSpan={6} className="px-4 pb-2.5 text-muted-foreground" data-testid="graph-source-note">
                    {source.why ? <p className="text-xs text-pretty [overflow-wrap:anywhere]">{source.why}</p> : null}
                    <Samples source={source} />
                  </td>
                </tr>
              ) : null}
            </tbody>
          ))}
        </PanelTable>
      )}
    </Panel>
  );
}

/** What agents got from the graph in the sessions and runs of the night's figures, with the worst ones to read. */
export function RetrievalPanel({ project, retrieval }: { project: string; retrieval: Retrieval | null }) {
  const t = useTranslations("curator.graph.retrieval");
  const format = useInsightFormat();
  const rate = (value: number | null) => (value === null ? t("noRate") : format.percent(value));
  return (
    <Panel title={t("title")} sub={t("sub")} testId="graph-retrieval">
      {retrieval === null || retrieval.used === 0 ? (
        <PanelLine testId="graph-retrieval-empty">{t("empty")}</PanelLine>
      ) : (
        <>
          <Facts>
            <Fact label={t("used")}>{format.count(retrieval.used)}</Fact>
            <Fact label={t("lookups")}>{format.count(retrieval.lookups)}</Fact>
            <Fact label={t("emptyLookups")} testId="graph-retrieval-empty-rate">
              {t("ofLookups", { rate: rate(retrieval.emptyRate), count: retrieval.empty })}
            </Fact>
            <Fact label={t("fallbacks")}>{t("ofEmpty", { rate: rate(retrieval.fallbackRate), count: retrieval.fallbacks })}</Fact>
            <Fact label={t("inTop")} testId="graph-retrieval-in-top">
              {t("ofEdited", { rate: rate(retrieval.inTopRate), count: retrieval.inTop, edited: retrieval.edited })}
            </Fact>
          </Facts>
          {retrieval.evidence.length > 0 ? (
            <div className="flex flex-col gap-2 border-t px-4 py-3">
              <h3 className="text-xs font-medium text-muted-foreground">{t("worst")}</h3>
              <ul className="flex flex-col gap-2">
                {retrieval.evidence.map((evidence) => (
                  <li key={evidence} className="flex min-w-0 flex-col gap-1">
                    <Identifier value={evidence} title={evidence} className="max-w-72" />
                    <EvidenceOpener project={project} evidence={evidence} testId="graph-retrieval-open" />
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </>
      )}
    </Panel>
  );
}

/** What the worker on duty last said of keeping the graph fresh: when it looked, and each source it reads. */
export function SyncPanel({ sync }: { sync: SyncState }) {
  const t = useTranslations("curator.graph.sync");
  const format = useInsightFormat();
  return (
    <Panel title={t("title")} sub={t("sub", { worker: sync.worker ?? "-", at: sync.checkedAt ? format.moment(sync.checkedAt) : t("never") })} testId="graph-sync">
      {sync.sources.length === 0 ? (
        <PanelLine>{t("none")}</PanelLine>
      ) : (
        <ul className="flex flex-col divide-y" aria-label={t("title")}>
          {sync.sources.map((source) => (
            <li key={source.source} className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-[13px]" data-testid="graph-sync-source" data-status={source.status}>
              <span className="font-mono text-xs text-foreground">{source.source}</span>
              <span className={cn(source.status === "failed" ? "font-medium text-danger" : "text-muted-foreground")}>{t.has(`status.${source.status}` as never) ? t(`status.${source.status}` as never) : source.status}</span>
              {source.commit ? <span className="font-mono text-xs text-muted-foreground">{shortCommit(source.commit)}</span> : null}
              {source.behind ? <span className="text-xs text-muted-foreground">{t("behind", { count: source.behind })}</span> : null}
              {source.error ? <span className="basis-full text-xs text-danger [overflow-wrap:anywhere]">{source.error}</span> : null}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
