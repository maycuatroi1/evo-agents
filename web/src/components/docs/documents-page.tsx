"use client";

import { BookOpen, Plus } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { DataCard } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { Identifier, NAME_LINK } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { Banner } from "@/components/feedback/banner";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { type DocsDocumentSummary, docsDocumentsQuery } from "./api";
import { useDocsViewer } from "./hooks";
import { documentHref, newDocumentHref } from "./links";
import { canWrite } from "./model";

const NARROW_HIDDEN = { updated_by: "hidden md:table-cell", updated_at: "hidden sm:table-cell" };

function DocumentsTable({ project, documents }: { project: string; documents: DocsDocumentSummary[] }) {
  const t = useTranslations("docs.documents");
  const columns = useMemo(() => {
    const helper = dataTableColumns<DocsDocumentSummary>();
    return helper.columns([
      helper.accessor("title", {
        header: () => t("columns.document"),
        sortFn: "alphanumeric",
        meta: { primary: true },
        cell: (info) => {
          const doc = info.row.original;
          return (
            <CellMain sub={<span className="font-mono">{doc.slug}</span>} subTitle={doc.slug}>
              <Link href={documentHref(project, doc.slug)} className={`${NAME_LINK} truncate`} data-document-link={doc.slug}>
                {doc.title}
              </Link>
            </CellMain>
          );
        },
      }),
      helper.accessor("latest_version", {
        header: () => t("columns.version"),
        sortFn: "basic",
        cell: (info) => <Identifier value={`v${info.getValue()}`} />,
      }),
      helper.accessor("updated_by", { header: () => t("columns.updatedBy"), cell: (info) => <span>{info.getValue()}</span> }),
      helper.accessor("updated_at", {
        header: () => t("columns.updatedAt"),
        sortFn: "datetime",
        meta: { numeric: true },
        cell: (info) => <When value={info.getValue()} />,
      }),
    ]);
  }, [t, project]);
  return (
    <DataTable
      data={documents}
      columns={columns}
      caption={t("caption", { project })}
      getRowId={(row) => row.slug}
      columnClassNames={NARROW_HIDDEN}
      testId="docs-documents-table"
      mobile={(doc) => ({
        title: doc.title,
        titleText: doc.title,
        href: documentHref(project, doc.slug),
        meta: [doc.slug, `v${doc.latest_version}`, doc.updated_by].join(", "),
        data: { "document-slug": doc.slug },
      })}
    />
  );
}

/**
 * The documents of a project (/p/{project}/x/docs/documents, the hub's feature docs; web/DESIGN.md, Plugins): each with
 * its latest version, who saved it and when. A writer makes a new one from here; a reader reads.
 */
export function DocumentsPage({ project, title, initialError }: { project: string; title: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("docs.documents");
  const state = useHubQuery(docsDocumentsQuery(browserApi, project), initialError);
  const viewer = useDocsViewer(project);
  const writer = canWrite(viewer);
  const create = writer ? (
    <Button asChild>
      <Link href={newDocumentHref(project)} data-testid="docs-new">
        <Plus aria-hidden="true" />
        {t("new")}
      </Link>
    </Button>
  ) : null;
  return (
    <div className="flex flex-col gap-6" data-testid="docs-documents">
      <PageHeader title={title} sub={t("sub")} actions={create} />
      {viewer && !writer ? (
        <Banner icon={BookOpen} title={t("readerTitle", { project })} testId="docs-read-only">
          {t("readerText")}
        </Banner>
      ) : null}
      <QueryView state={state} loading={<TableSkeleton rows={3} />}>
        {(documents) =>
          documents.length === 0 ? (
            <EmptyState
              icon={BookOpen}
              title={t("emptyTitle")}
              description={writer ? t("emptyWriter") : t("emptyReader")}
              testId="docs-documents-empty"
            >
              {create}
            </EmptyState>
          ) : (
            <section aria-labelledby="docs-documents-title">
              <h2 id="docs-documents-title" className="sr-only">
                {t("caption", { project })}
              </h2>
              <DataCard testId="docs-documents-card">
                <DocumentsTable project={project} documents={documents} />
              </DataCard>
            </section>
          )
        }
      </QueryView>
    </div>
  );
}
