"use client";

import { FileText, Settings, Upload } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { DataCard } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK, Tag } from "@/components/data/identifier";
import { TabNav } from "@/components/data/tab-nav";
import { When } from "@/components/data/when";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { type SrsDocumentSummary, srsDocumentsQuery } from "./api";
import { VersionBadge, VersionChip } from "./badges";
import { ReaderNote } from "./banner";
import { useSrsViewer } from "./hooks";
import { documentHref, documentsHref, importHref, settingsHref } from "./links";
import { canWrite } from "./model";

const NARROW_HIDDEN = { kind: "hidden md:table-cell", created_by: "hidden lg:table-cell", created_at: "hidden sm:table-cell" };

/** The tabs of the plugin's own pages: its documents, and the import for writers. */
export function SrsTabs({ project, current, writer }: { project: string; current: "documents" | "import"; writer: boolean }) {
  const t = useTranslations("srs.documents");
  const tabs = [
    { id: "documents", label: t("tabDocuments"), href: documentsHref(project), icon: FileText },
    ...(writer || current === "import" ? [{ id: "import", label: t("tabImport"), href: importHref(project), icon: Upload }] : []),
  ];
  return <TabNav label={t("tabsLabel")} tabs={tabs} current={current} testId="srs-tabs" tabTestId="srs-tab" />;
}

function VersionCell({ number, state, none }: { number: number | null; state: SrsDocumentSummary["latest_state"] | null; none: string }) {
  if (number === null || state === null) return <span className="text-xs text-fg-subtle">{none}</span>;
  return (
    <span className="inline-flex items-center gap-1.5">
      <VersionChip number={number} />
      <VersionBadge state={state} />
    </span>
  );
}

function DocumentsTable({ project, documents }: { project: string; documents: SrsDocumentSummary[] }) {
  const t = useTranslations("srs.documents");
  const tKind = useTranslations("srs.documentKind");
  const columns = useMemo(() => {
    const helper = dataTableColumns<SrsDocumentSummary>();
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
      helper.accessor("kind", { header: () => t("columns.kind"), cell: (info) => <Tag>{tKind(info.getValue())}</Tag> }),
      helper.accessor("approved_version", {
        header: () => t("columns.approved"),
        cell: (info) => (
          <VersionCell number={info.getValue()} state={info.getValue() === null ? null : "approved"} none={t("noneApproved")} />
        ),
      }),
      helper.accessor("latest_version", {
        header: () => t("columns.latest"),
        cell: (info) => <VersionCell number={info.getValue()} state={info.row.original.latest_state} none={t("noVersion")} />,
      }),
      helper.accessor("created_by", { header: () => t("columns.createdBy"), cell: (info) => <span>{info.getValue()}</span> }),
      helper.accessor("created_at", {
        header: () => t("columns.createdAt"),
        sortFn: "datetime",
        meta: { numeric: true },
        cell: (info) => <When value={info.getValue()} />,
      }),
    ]);
  }, [t, tKind, project]);
  return (
    <DataTable
      data={documents}
      columns={columns}
      caption={t("caption", { project })}
      getRowId={(row) => row.slug}
      columnClassNames={NARROW_HIDDEN}
      testId="srs-documents-table"
      mobile={(doc) => ({
        title: doc.title,
        titleText: doc.title,
        href: documentHref(project, doc.slug),
        status: doc.latest_state ? <VersionBadge state={doc.latest_state} /> : undefined,
        meta: [
          doc.slug,
          doc.approved_version ? t("approvedShort", { version: doc.approved_version }) : t("noneApproved"),
          doc.latest_version ? t("latestShort", { version: doc.latest_version }) : null,
        ]
          .filter(Boolean)
          .join(", "),
        data: { "document-slug": doc.slug },
      })}
    />
  );
}

/**
 * The BA documents of a project (/p/{project}/x/srs/documents, the core plugin srs; web/DESIGN.md, Plugins): each with
 * its kind, the version approved last and the latest with its state. A writer imports Markdown from here; a reader
 * reads. An admin of the project sets the plugin's template, roles and approval policy on its page in the project.
 */
export function DocumentsPage({ project, title, initialError }: { project: string; title: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("srs.documents");
  const state = useHubQuery(srsDocumentsQuery(browserApi, project), initialError);
  const viewer = useSrsViewer(project);
  const writer = canWrite(viewer);
  return (
    <div className="flex flex-col gap-6" data-testid="srs-documents">
      <div className="flex flex-col gap-4">
        <PageHeader
          title={title}
          actions={
            <>
              <Button asChild variant="ghost">
                <Link href={settingsHref(project)} data-testid="srs-settings">
                  <Settings aria-hidden="true" />
                  {t("settings")}
                </Link>
              </Button>
              {writer ? (
                <Button asChild>
                  <Link href={importHref(project)} data-testid="srs-import-open">
                    <Upload aria-hidden="true" />
                    {t("import")}
                  </Link>
                </Button>
              ) : null}
            </>
          }
        />
        <SrsTabs project={project} current="documents" writer={writer} />
      </div>
      {viewer && !writer ? <ReaderNote project={project}>{t("readerText")}</ReaderNote> : null}
      <QueryView state={state} loading={<TableSkeleton rows={3} />}>
        {(documents) =>
          documents.length === 0 ? (
            <EmptyState
              icon={FileText}
              title={t("emptyTitle")}
              description={writer ? t("emptyWriter") : t("emptyReader")}
              testId="srs-documents-empty"
            >
              {writer ? (
                <Button asChild>
                  <Link href={importHref(project)}>
                    <Upload aria-hidden="true" />
                    {t("import")}
                  </Link>
                </Button>
              ) : null}
            </EmptyState>
          ) : (
            <section aria-labelledby="srs-documents-title">
              <h2 id="srs-documents-title" className="sr-only">
                {t("caption", { project })}
              </h2>
              <DataCard testId="srs-documents-card">
                <DocumentsTable project={project} documents={documents} />
              </DataCard>
            </section>
          )
        }
      </QueryView>
    </div>
  );
}
