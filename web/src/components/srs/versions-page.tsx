"use client";

import { Bot, FileDiff } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import { DataCard } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { RunRef, Tag } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { projectHref } from "@/components/shell/nav";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { PageSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { type SrsDocument, type SrsVersion, srsDocumentQuery } from "./api";
import { VersionBadge, VersionChip } from "./badges";
import { DocumentHead } from "./document-head";
import { compareHref, documentHref, reviewHref } from "./links";
import { comparePair, returnedBy } from "./model";

const NARROW_HIDDEN = { author: "hidden md:table-cell", reviews: "hidden lg:table-cell", created_at: "hidden sm:table-cell" };

/** Two versions to compare, as a form whose button opens the comparison. */
function ComparePicker({ project, doc }: { project: string; doc: SrsDocument }) {
  const t = useTranslations("srs.versions");
  const ids = useId();
  const router = useRouter();
  const numbers = doc.versions.map((version) => version.number);
  const pair = comparePair(doc, null, null);
  const [from, setFrom] = useState(pair?.from ?? numbers[0]);
  const [to, setTo] = useState(pair?.to ?? numbers[0]);
  if (numbers.length < 2) return <p className="text-[13px] text-muted-foreground">{t("oneVersion")}</p>;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    router.push(compareHref(project, doc.slug, Math.min(from, to), Math.max(from, to)));
  };
  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-3" data-testid="srs-compare-picker">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-from`}>{t("from")}</Label>
        <NativeSelect id={`${ids}-from`} value={from} onChange={(event) => setFrom(Number(event.target.value))}>
          {numbers.map((number) => (
            <option key={number} value={number}>{`v${number}`}</option>
          ))}
        </NativeSelect>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-to`}>{t("to")}</Label>
        <NativeSelect id={`${ids}-to`} value={to} onChange={(event) => setTo(Number(event.target.value))}>
          {numbers.map((number) => (
            <option key={number} value={number}>{`v${number}`}</option>
          ))}
        </NativeSelect>
      </div>
      <Button type="submit" variant="secondary" disabled={from === to} data-testid="srs-compare-open">
        <FileDiff aria-hidden="true" />
        {t("compare")}
      </Button>
      <p className="basis-full text-xs text-fg-subtle">{t("compareHint")}</p>
    </form>
  );
}

function Reviews({ version }: { version: SrsVersion }) {
  const t = useTranslations("srs.versions");
  const tRole = useTranslations("srs.role");
  const round = version.reviews.filter((review) => review.round === version.round);
  if (round.length === 0) return <span className="text-xs text-fg-subtle">{t("noReviews")}</span>;
  return (
    <span className="flex flex-wrap gap-1">
      {round.map((review) => (
        <Tag key={`${review.reviewer}-${review.created_at}`} className={review.decision === "approve" ? "bg-success-soft text-success" : "bg-attention-soft text-attention"}>
          {review.role ? `${tRole(review.role)} ` : ""}
          {review.reviewer}
          <span className="sr-only">{review.decision === "approve" ? t("approvedBy") : t("changesBy")}</span>
        </Tag>
      ))}
    </span>
  );
}

function VersionsTable({ project, doc }: { project: string; doc: SrsDocument }) {
  const t = useTranslations("srs.versions");
  const columns = useMemo(() => {
    const helper = dataTableColumns<SrsVersion>();
    return helper.columns([
      helper.accessor("number", {
        header: () => t("columns.version"),
        sortFn: "basic",
        cell: (info) => <VersionChip number={info.getValue()} href={documentHref(project, doc.slug, { v: info.getValue() })} />,
      }),
      helper.accessor("state", { header: () => t("columns.state"), cell: (info) => <VersionBadge state={info.getValue()} /> }),
      helper.accessor("summary", {
        header: () => t("columns.summary"),
        meta: { primary: true },
        cell: (info) => {
          const version = info.row.original;
          const returned = returnedBy(version);
          const sub = returned ? t("returned", { reviewer: returned.reviewer, note: returned.body || t("noNote") }) : undefined;
          return (
            <CellMain sub={sub} subTitle={sub}>
              <Link href={reviewHref(project, doc.slug, version.number)} className="truncate font-medium text-foreground hover:text-brand">
                {version.summary || t("noSummary")}
              </Link>
            </CellMain>
          );
        },
      }),
      helper.accessor("author", {
        header: () => t("columns.author"),
        cell: (info) => (
          <span className="inline-flex items-center gap-1.5">
            {info.getValue()}
            {info.row.original.drafted_by_run ? (
              <span className="inline-flex items-center gap-1 text-xs text-fg-subtle">
                <Bot className="size-3.5" aria-hidden="true" />
                <RunRef id={info.row.original.drafted_by_run} href={projectHref(project, `runs/${info.row.original.drafted_by_run}`)} label={t("openRun", { id: info.row.original.drafted_by_run })} />
              </span>
            ) : null}
          </span>
        ),
      }),
      helper.display({ id: "reviews", header: () => t("columns.reviews"), cell: (info) => <Reviews version={info.row.original} /> }),
      helper.accessor("created_at", {
        header: () => t("columns.saved"),
        sortFn: "datetime",
        meta: { numeric: true },
        cell: (info) => <When value={info.getValue()} />,
      }),
    ]);
  }, [t, project, doc.slug]);
  return (
    <DataTable
      data={doc.versions}
      columns={columns}
      caption={t("caption", { title: doc.title })}
      getRowId={(row) => String(row.number)}
      columnClassNames={NARROW_HIDDEN}
      testId="srs-versions-table"
      mobile={(version) => ({
        title: `v${version.number}: ${version.summary || t("noSummary")}`,
        href: reviewHref(project, doc.slug, version.number),
        status: <VersionBadge state={version.state} />,
        meta: version.author,
        data: { version: version.number },
      })}
    />
  );
}

/**
 * A document's versions (/p/{project}/x/srs/documents/{slug}/versions), the newest first: each with its state, why
 * it was saved, who wrote it (and the agent's run that drafted it), the reviews of its round and when, and a form that
 * compares two of them. A version's number opens its contents, its summary its review.
 */
export function VersionsPage({ project, slug, initialError }: { project: string; slug: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("srs.versions");
  const state = useHubQuery(srsDocumentQuery(browserApi, project, slug, null), initialError);
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(doc) => (
        <div className="flex flex-col gap-6" data-testid="srs-versions">
          <DocumentHead project={project} doc={doc} current="versions" version={doc.latest_version} />
          <ComparePicker project={project} doc={doc} />
          <section aria-labelledby="srs-versions-title">
            <h2 id="srs-versions-title" className="sr-only">
              {t("caption", { title: doc.title })}
            </h2>
            <DataCard>
              <VersionsTable project={project} doc={doc} />
            </DataCard>
          </section>
        </div>
      )}
    </QueryView>
  );
}
