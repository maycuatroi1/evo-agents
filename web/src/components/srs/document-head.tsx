"use client";

import { useQuery } from "@tanstack/react-query";
import { BookOpen, ClipboardCheck, Download, FileDiff, History, MessageSquareText } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { Tag } from "@/components/data/identifier";
import { TabNav } from "@/components/data/tab-nav";
import { useVisibilityName } from "@/components/data/visibility";
import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";

import { exportMarkdown, type SrsDocument, srsCommentsQuery } from "./api";
import { CodeChip } from "./badges";
import { signInAgainOn401 } from "./hooks";
import { compareHref, commentsHref, documentHref, reviewHref, versionsHref } from "./links";
import { inReview } from "./model";

export type DocumentTab = "read" | "versions" | "review" | "comments";

/** A version as a Markdown file the browser saves, named after the document and the version. */
function ExportButton({ project, doc, version }: { project: string; doc: SrsDocument; version: number }) {
  const t = useTranslations("srs.head");
  const failure = useWriteFailure();
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      const exported = await exportMarkdown(browserApi(), project, doc.slug, version);
      const url = URL.createObjectURL(new Blob([exported.markdown], { type: "text/markdown;charset=utf-8" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = `${doc.slug}-v${exported.version}.md`;
      link.click();
      URL.revokeObjectURL(url);
      notify({ tone: "success", text: t("exported", { version: exported.version }), description: link.download });
    } catch (error) {
      signInAgainOn401(error);
      notifyFailure(t("exportFailed", { version }), failure(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Button type="button" variant="secondary" busy={busy} onClick={() => void save()} data-testid="srs-export">
      <Download aria-hidden="true" />
      {t("export", { version })}
    </Button>
  );
}

/**
 * The head of every page of one document: its title, slug and kind, a line saying its template, visibility and which
 * versions are approved, in review and latest, Compare and Export Markdown, then the document's tabs (Read, Versions,
 * Review of the version in review, Comments with the open threads). `title` replaces the document's own h1 on a page
 * that names something else (a version's review, a comparison).
 */
export function DocumentHead({
  project,
  doc,
  current,
  version,
  title,
  status,
  actions,
}: {
  project: string;
  doc: SrsDocument;
  current: DocumentTab | null;
  /** The version the page shows, which Export saves. */
  version: number | null;
  title?: ReactNode;
  status?: ReactNode;
  /** The page's own actions in place of Compare and Export; null for none. */
  actions?: ReactNode;
}) {
  const t = useTranslations("srs.head");
  const tKind = useTranslations("srs.documentKind");
  const visibility = useVisibilityName();
  const comments = useQuery({ ...srsCommentsQuery(browserApi, project, doc.slug), retry: false });
  const open = (comments.data ?? []).filter((comment) => comment.parent_id === null && comment.resolved_by === null).length;
  const review = inReview(doc);
  const latest = doc.latest_version;
  const approved = doc.approved_version;
  const level = typeof doc.label.level === "string" ? doc.label.level : null;
  const sub = [
    t("template", { template: doc.template }),
    level ? t("visibility", { level: visibility(level) }) : null,
    approved ? t("approved", { version: approved }) : t("noneApproved"),
    review ? t("inReview", { version: review.number }) : null,
    latest ? t("latest", { version: latest }) : null,
  ].filter(Boolean);
  const tabs = [
    { id: "read", label: t("tabs.read"), href: documentHref(project, doc.slug), icon: BookOpen },
    { id: "versions", label: t("tabs.versions"), href: versionsHref(project, doc.slug), icon: History },
    ...(review || latest
      ? [
          {
            id: "review",
            label: review ? t("tabs.reviewOf", { version: review.number }) : t("tabs.review"),
            href: reviewHref(project, doc.slug, review?.number ?? latest!),
            icon: ClipboardCheck,
          },
        ]
      : []),
    {
      id: "comments",
      label: t("tabs.comments"),
      href: commentsHref(project, doc.slug),
      icon: MessageSquareText,
      count: open,
      countLabel: t("tabs.open", { count: open }),
    },
  ];
  const compare = approved && latest && approved !== latest ? compareHref(project, doc.slug, approved, latest) : null;
  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={title ?? doc.title}
        status={status}
        tags={
          title ? null : (
            <>
              <CodeChip code={doc.slug} />
              <Tag>{tKind(doc.kind)}</Tag>
            </>
          )
        }
        sub={title ? t("of", { title: doc.title }) : sub.join(", ")}
        actions={
          actions !== undefined ? actions : (
            <>
              {compare ? (
                <Button asChild variant="secondary">
                  <Link href={compare} data-testid="srs-compare-latest">
                    <FileDiff aria-hidden="true" />
                    {t("compare", { from: approved!, to: latest! })}
                  </Link>
                </Button>
              ) : null}
              {version ? <ExportButton project={project} doc={doc} version={version} /> : null}
            </>
          )
        }
        testId="srs-document-head"
      />
      <TabNav label={t("tabs.label", { title: doc.title })} tabs={tabs} current={current} testId="srs-document-tabs" tabTestId="srs-tab" />
    </div>
  );
}
