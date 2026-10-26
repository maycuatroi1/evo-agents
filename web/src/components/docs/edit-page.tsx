"use client";

import { BookOpen } from "lucide-react";
import { useTranslations } from "next-intl";

import { Banner } from "@/components/feedback/banner";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { PageSkeleton } from "@/components/states/states";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { docsDocumentQuery } from "./api";
import { DocumentHead } from "./document-head";
import { DocumentEditor } from "./editor";
import { useDocsViewer } from "./hooks";
import { canWrite } from "./model";

/** What a reader sees where a writer edits: the hub would refuse the save, so no editor. */
function ReadersRead({ project }: { project: string }) {
  const t = useTranslations("docs.editor");
  return (
    <Banner icon={BookOpen} title={t("readerTitle", { project })} testId="docs-read-only">
      {t("readerText")}
    </Banner>
  );
}

/** The editor of a document's next version (/p/{project}/x/docs/documents/{slug}/edit), on its latest version. */
export function EditPage({ project, slug, initialError }: { project: string; slug: string; initialError: ApiErrorInfo | null }) {
  const state = useHubQuery(docsDocumentQuery(browserApi, project, slug, null), initialError);
  const viewer = useDocsViewer(project);
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(doc) => (
        <div className="flex flex-col gap-6" data-testid="docs-edit-page">
          <DocumentHead project={project} doc={doc} current="edit" />
          {viewer && !canWrite(viewer) ? <ReadersRead project={project} /> : <DocumentEditor project={project} doc={doc} />}
        </div>
      )}
    </QueryView>
  );
}

/** A new document of the project (/p/{project}/x/docs/new): its title, slug and Markdown. */
export function NewDocumentPage({ project }: { project: string }) {
  const t = useTranslations("docs.editor");
  const viewer = useDocsViewer(project);
  return (
    <div className="flex flex-col gap-6" data-testid="docs-new-page">
      <PageHeader title={t("newTitle")} sub={t("newSub", { project })} />
      {viewer && !canWrite(viewer) ? <ReadersRead project={project} /> : <DocumentEditor project={project} doc={null} />}
    </div>
  );
}
