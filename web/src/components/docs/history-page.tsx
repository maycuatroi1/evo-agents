"use client";

import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { DataCard } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { Identifier } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { PageSkeleton } from "@/components/states/states";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { type DocsDocument, type DocsVersion, docsDocumentQuery } from "./api";
import { DocumentHead } from "./document-head";
import { documentHref } from "./links";

const NARROW_HIDDEN = { author: "hidden md:table-cell", created_at: "hidden sm:table-cell" };

function VersionsTable({ project, doc }: { project: string; doc: DocsDocument }) {
  const t = useTranslations("docs.history");
  const columns = useMemo(() => {
    const helper = dataTableColumns<DocsVersion>();
    return helper.columns([
      helper.accessor("number", {
        header: () => t("columns.version"),
        sortFn: "basic",
        cell: (info) => <Identifier value={`v${info.getValue()}`} href={documentHref(project, doc.slug, info.getValue())} />,
      }),
      helper.accessor("summary", {
        header: () => t("columns.summary"),
        meta: { primary: true },
        cell: (info) => {
          const version = info.row.original;
          return (
            <CellMain sub={version.title} subTitle={version.title}>
              <span className="truncate">{version.summary || t("noSummary")}</span>
            </CellMain>
          );
        },
      }),
      helper.accessor("author", { header: () => t("columns.author"), cell: (info) => <span>{info.getValue()}</span> }),
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
      testId="docs-versions-table"
      mobile={(version) => ({
        title: `v${version.number}: ${version.summary || t("noSummary")}`,
        href: documentHref(project, doc.slug, version.number),
        meta: version.author,
        data: { version: version.number },
      })}
    />
  );
}

/**
 * A document's history (/p/{project}/x/docs/documents/{slug}/history), the newest version first: what each changed,
 * its title, who saved it and when. A version's number opens it.
 */
export function HistoryPage({ project, slug, initialError }: { project: string; slug: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("docs.history");
  const state = useHubQuery(docsDocumentQuery(browserApi, project, slug, null), initialError);
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(doc) => (
        <div className="flex flex-col gap-6" data-testid="docs-history">
          <DocumentHead project={project} doc={doc} current="history" />
          <section aria-labelledby="docs-history-title">
            <h2 id="docs-history-title" className="sr-only">
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
