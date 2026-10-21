"use client";

import { MessageSquareText } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Segmented } from "@/components/data/segmented";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, ListSkeleton, PageSkeleton } from "@/components/states/states";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { type SrsDocument, srsCommentsQuery, srsDocumentQuery } from "./api";
import { ReaderNote } from "./banner";
import { CommentForm, ThreadView } from "./comments";
import { DocumentHead } from "./document-head";
import { useSrsViewer } from "./hooks";
import { canWrite, threadsOf } from "./model";

type Filter = "open" | "resolved" | "all";

function Threads({ project, doc }: { project: string; doc: SrsDocument }) {
  const t = useTranslations("srs.comments");
  const viewer = useSrsViewer(project);
  const writer = canWrite(viewer);
  const [filter, setFilter] = useState<Filter>("open");
  const state = useHubQuery(srsCommentsQuery(browserApi, project, doc.slug));
  return (
    <div className="flex flex-col gap-4">
      {viewer && !writer ? <ReaderNote project={project}>{t("readerText")}</ReaderNote> : null}
      <div className="flex flex-wrap items-center gap-3">
        <Segmented
          label={t("filter")}
          value={filter}
          onChange={setFilter}
          options={[
            { value: "open", label: t("filters.open"), testId: "srs-comments-open" },
            { value: "resolved", label: t("filters.resolved"), testId: "srs-comments-resolved" },
            { value: "all", label: t("filters.all"), testId: "srs-comments-all" },
          ]}
        />
        <p className="text-[13px] text-muted-foreground">{t("follows")}</p>
      </div>
      <QueryView state={state} loading={<ListSkeleton rows={3} />}>
        {(comments) => {
          const threads = threadsOf(comments).filter((thread) =>
            filter === "all" ? true : filter === "open" ? thread.root.resolved_by === null : thread.root.resolved_by !== null,
          );
          return threads.length === 0 ? (
            <EmptyState icon={MessageSquareText} title={t(`empty.${filter}`)} description={t("emptyText")} testId="srs-comments-empty" />
          ) : (
            <div className="flex flex-col gap-3" data-testid="srs-threads">
              {threads.map((thread) => (
                <ThreadView key={thread.root.id} project={project} slug={doc.slug} thread={thread} writer={writer} />
              ))}
            </div>
          );
        }}
      </QueryView>
      {writer ? (
        <div className="rounded-md border bg-card p-4">
          <CommentForm project={project} slug={doc.slug} target={{}} label={t("onDocumentLabel", { title: doc.title })} submitLabel={t("comment")} />
        </div>
      ) : null}
    </div>
  );
}

/**
 * A document's comments (/p/{project}/x/srs/documents/{slug}/comments) as threads, open ones first by default: what
 * each is about, its messages and, for writers, an answer box and Resolve; and a comment on the whole document.
 */
export function CommentsPage({ project, slug, initialError }: { project: string; slug: string; initialError: ApiErrorInfo | null }) {
  const state = useHubQuery(srsDocumentQuery(browserApi, project, slug, null), initialError);
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(doc) => (
        <div className="flex flex-col gap-6" data-testid="srs-comments">
          <DocumentHead project={project} doc={doc} current="comments" version={doc.approved_version ?? doc.latest_version} />
          <Threads project={project} doc={doc} />
        </div>
      )}
    </QueryView>
  );
}
