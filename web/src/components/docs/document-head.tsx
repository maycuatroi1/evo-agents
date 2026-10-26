"use client";

import { BookOpen, History, Pencil } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Identifier } from "@/components/data/identifier";
import { TabNav } from "@/components/data/tab-nav";
import { useVisibilityName } from "@/components/data/visibility";
import { When } from "@/components/data/when";
import { PageHeader } from "@/components/shell/page-header";
import { Button } from "@/components/ui/button";

import type { DocsDocument } from "./api";
import { useDocsViewer } from "./hooks";
import { documentHref, editHref, historyHref } from "./links";
import { canWrite } from "./model";

export type DocumentTab = "read" | "history" | "edit";

/**
 * The head of every page of one document: its title and slug, one line saying the version shown, who saved it and
 * when, and who may read it, Edit for a writer, then the document's tabs (Read, History). `shown` is the version the
 * page shows; the latest when left out.
 */
export function DocumentHead({
  project,
  doc,
  current,
  shown,
  actions,
}: {
  project: string;
  doc: DocsDocument;
  current: DocumentTab;
  shown?: { number: number; author: string; created_at: string };
  actions?: ReactNode;
}) {
  const t = useTranslations("docs.head");
  const visibility = useVisibilityName();
  const writer = canWrite(useDocsViewer(project));
  const version = shown ?? { number: doc.latest_version, author: doc.updated_by, created_at: doc.updated_at };
  const level = typeof doc.label.level === "string" ? doc.label.level : null;
  const sub = (
    <>
      {t("version", { number: version.number, latest: doc.latest_version, author: version.author })}{" "}
      <When value={version.created_at} />
      {level ? `, ${t("visibility", { level: visibility(level) })}` : null}
    </>
  );
  const edit =
    writer && current !== "edit" ? (
      <Button asChild variant="secondary">
        <Link href={editHref(project, doc.slug)} data-testid="docs-edit">
          <Pencil aria-hidden="true" />
          {t("edit")}
        </Link>
      </Button>
    ) : null;
  const tabs = [
    { id: "read", label: t("tabs.read"), href: documentHref(project, doc.slug), icon: BookOpen },
    {
      id: "history",
      label: t("tabs.history"),
      href: historyHref(project, doc.slug),
      icon: History,
      count: doc.versions.length,
      countLabel: t("tabs.versions", { count: doc.versions.length }),
    },
    ...(current === "edit" ? [{ id: "edit", label: t("tabs.edit"), href: editHref(project, doc.slug), icon: Pencil }] : []),
  ];
  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={doc.title}
        tags={<Identifier value={doc.slug} testId="docs-slug" />}
        sub={sub}
        actions={
          <>
            {actions}
            {edit}
          </>
        }
        testId="docs-document-head"
      />
      <TabNav label={t("tabs.label", { title: doc.title })} tabs={tabs} current={current} testId="docs-document-tabs" tabTestId="docs-tab" />
    </div>
  );
}
