"use client";

import { ArrowRight, ClipboardCheck, Columns2, FileDiff, Minus, MoveRight, PencilLine, Plus, Rows3 } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { MetricStrip } from "@/components/data/metric-strip";
import { Segmented } from "@/components/data/segmented";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, ListSkeleton, PageSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { useIsMobile } from "@/hooks/use-mobile";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { type SrsDiff, type SrsDiffItem, type SrsDocument, srsDiffQuery, srsDocumentQuery } from "./api";
import { ChangeBadge, CodeChip, KindTag, VersionBadge } from "./badges";
import { changeCounts } from "./content-model";
import { type DiffMode, DiffLines } from "./diff-view";
import { DocumentHead } from "./document-head";
import { itemHref, reviewHref, versionsHref } from "./links";
import { comparePair, versionOf } from "./model";

function ItemDiff({ project, slug, item, mode, diff }: { project: string; slug: string; item: SrsDiffItem; mode: DiffMode; diff: SrsDiff }) {
  const t = useTranslations("srs.compare");
  const title = item.title_to ?? item.title_from ?? item.key;
  const renamed = item.title_from && item.title_to && item.title_from !== item.title_to;
  const href = item.code && item.change !== "removed" ? itemHref(project, slug, item.code, diff.to_version) : undefined;
  return (
    <article className="overflow-hidden rounded-md border bg-card" aria-labelledby={`diff-${item.key}`} data-testid="srs-diff-item" data-change={item.change} data-item-key={item.key}>
      <header className="flex flex-wrap items-center gap-2 px-4 py-3">
        {item.code ? <CodeChip code={item.code} href={href} /> : null}
        <h3 id={`diff-${item.key}`} className="min-w-0 text-sm font-medium [overflow-wrap:anywhere]">
          {title}
        </h3>
        <KindTag kind={item.kind} />
        <ChangeBadge change={item.change} />
        {renamed ? <p className="basis-full text-xs text-muted-foreground">{t("renamed", { from: item.title_from!, to: item.title_to! })}</p> : null}
      </header>
      {item.change === "moved" ? (
        <p className="border-t px-4 py-3 text-[13px] text-muted-foreground">{t("movedText")}</p>
      ) : (
        <DiffLines lines={item.lines} mode={mode} from={diff.from_version} to={diff.to_version} label={t("linesOf", { name: item.code ?? title })} />
      )}
    </article>
  );
}

function Changes({ project, doc, pair }: { project: string; doc: SrsDocument; pair: { from: number; to: number } }) {
  const t = useTranslations("srs.compare");
  const [mode, setMode] = useState<DiffMode>("unified");
  const mobile = useIsMobile();
  const shownMode: DiffMode = mobile ? "unified" : mode;
  const state = useHubQuery(srsDiffQuery(browserApi, project, doc.slug, pair.from, pair.to));
  return (
    <QueryView state={state} loading={<ListSkeleton rows={4} />}>
      {(diff) => {
        const counts = changeCounts(diff.items);
        return (
          <div className="flex flex-col gap-4" data-testid="srs-diff">
            <MetricStrip
              label={t("countsLabel", { from: pair.from, to: pair.to })}
              testId="srs-diff-counts"
              metrics={[
                { id: "items", label: t("counts.items"), icon: FileDiff, value: diff.items.length },
                { id: "changed", label: t("counts.changed"), icon: PencilLine, value: counts.changed },
                { id: "added", label: t("counts.added"), icon: Plus, value: counts.added },
                { id: "removed", label: t("counts.removed"), icon: Minus, value: counts.removed },
                { id: "moved", label: t("counts.moved"), icon: MoveRight, value: counts.moved },
              ]}
            />
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-[13px] text-muted-foreground">{t("matched")}</p>
              <Segmented
                label={t("mode")}
                value={mode}
                onChange={setMode}
                className="hidden md:inline-flex"
                options={[
                  { value: "unified", label: t("unified"), icon: Rows3, testId: "srs-diff-unified" },
                  { value: "split", label: t("split"), icon: Columns2, testId: "srs-diff-split" },
                ]}
              />
            </div>
            {diff.preamble.length ? (
              <article className="overflow-hidden rounded-md border bg-card" aria-labelledby="diff-preamble">
                <h3 id="diff-preamble" className="px-4 py-3 text-sm font-medium">
                  {t("preamble")}
                </h3>
                <DiffLines lines={diff.preamble} mode={shownMode} from={diff.from_version} to={diff.to_version} label={t("preamble")} />
              </article>
            ) : null}
            {diff.items.length === 0 && diff.preamble.length === 0 ? (
              <EmptyState icon={FileDiff} title={t("sameTitle")} description={t("sameText", { from: pair.from, to: pair.to })} testId="srs-diff-same" />
            ) : (
              diff.items.map((item) => <ItemDiff key={item.key} project={project} slug={doc.slug} item={item} mode={shownMode} diff={diff} />)
            )}
          </div>
        );
      }}
    </QueryView>
  );
}

/**
 * Two versions of a document compared item by item (/p/{project}/x/srs/documents/{slug}/compare?from=&to=): items are
 * matched by their key, so a renamed or renumbered item shows as changed, not as removed and added; how many items
 * changed, were added, removed or moved; each item's lines, unified or side by side. Without a pair it compares the
 * approved version with the latest.
 */
export function ComparePage({ project, slug, from, to, initialError }: { project: string; slug: string; from: number | null; to: number | null; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("srs.compare");
  const state = useHubQuery(srsDocumentQuery(browserApi, project, slug, null), initialError);
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(doc) => {
        const pair = comparePair(doc, from, to);
        const target = pair ? versionOf(doc, pair.to) : null;
        const older = pair ? versionOf(doc, pair.from) : null;
        return (
          <div className="flex flex-col gap-6" data-testid="srs-compare">
            <DocumentHead
              project={project}
              doc={doc}
              current="versions"
              version={pair?.to ?? doc.latest_version}
              title={pair ? t("title", { from: pair.from, to: pair.to }) : t("titleNone")}
              status={
                older && target ? (
                  <span className="inline-flex items-center gap-1.5">
                    <VersionBadge state={older.state} />
                    <ArrowRight className="size-4 text-muted-foreground" aria-hidden="true" />
                    <span className="sr-only">{t("to")}</span>
                    <VersionBadge state={target.state} />
                  </span>
                ) : null
              }
              actions={
                target?.state === "in_review" ? (
                  <Button asChild>
                    <Link href={reviewHref(project, slug, target.number)} data-testid="srs-compare-review">
                      <ClipboardCheck aria-hidden="true" />
                      {t("review", { version: target.number })}
                    </Link>
                  </Button>
                ) : null
              }
            />
            {pair ? (
              <Changes project={project} doc={doc} pair={pair} />
            ) : (
              <EmptyState icon={FileDiff} title={t("oneTitle")} description={t("oneText")} testId="srs-compare-one">
                <Button asChild variant="secondary">
                  <Link href={versionsHref(project, slug)}>{t("versions")}</Link>
                </Button>
              </EmptyState>
            )}
          </div>
        );
      }}
    </QueryView>
  );
}
