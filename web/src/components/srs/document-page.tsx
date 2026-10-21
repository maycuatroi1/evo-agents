"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, FileText, Pencil } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ReactNode, useMemo } from "react";

import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, PageSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { type SrsDiffItem, type SrsDocument, type SrsItem, srsDiffQuery, srsDocumentQuery } from "./api";
import { ChangeBadge, CodeChip, KindTag } from "./badges";
import { ReaderNote } from "./banner";
import { DocumentHead } from "./document-head";
import { useSrsViewer } from "./hooks";
import { ItemBody, type ItemLinks } from "./item-body";
import { ItemEditor } from "./item-editor";
import { documentHref, itemHref } from "./links";
import { canWrite, CODED_KINDS, findItem, firstItem, shownNumber, versionOf } from "./model";
import { DraftBanner } from "./submit-banner";
import { Toc, TocDisclosure } from "./toc";
import { ChecksCard, CommentsCard, VersionCard } from "./version-rail";
import { VersionMenu } from "./version-menu";

type Asked = { version: number | null; item: string | null; edit: boolean };

function ItemHead({ item, change, actions }: { item: SrsItem; change: SrsDiffItem["change"] | undefined; actions: ReactNode }) {
  return (
    <header className="flex flex-wrap items-center gap-x-3 gap-y-2">
      {item.code ? <CodeChip code={item.code} /> : null}
      <h2 id="srs-item-title" className="min-w-0 text-lg leading-7 font-semibold tracking-tight text-balance [overflow-wrap:anywhere]" data-testid="srs-item-title">
        {item.title}
      </h2>
      <KindTag kind={item.kind} />
      {change ? <ChangeBadge change={change} /> : null}
      <div className="ml-auto flex flex-wrap gap-2">{actions}</div>
    </header>
  );
}

function DocumentBody({ project, latest, asked }: { project: string; latest: SrsDocument; asked: Asked }) {
  const t = useTranslations("srs.read");
  const router = useRouter();
  const viewer = useSrsViewer(project);
  const writer = canWrite(viewer);
  const slug = latest.slug;
  const shown = shownNumber(latest, asked.version);
  const isLatest = shown === latest.latest_version;
  const state = useHubQuery(srsDocumentQuery(browserApi, project, slug, isLatest ? null : shown));
  const approved = latest.approved_version;
  const diff = useQuery({
    ...srsDiffQuery(browserApi, project, slug, approved ?? 0, shown ?? 0),
    enabled: Boolean(approved && shown && approved !== shown),
    retry: false,
  });
  const changes = useMemo(() => new Map((diff.data?.items ?? []).map((item) => [item.key, item.change])), [diff.data]);
  const latestVersion = versionOf(latest, latest.latest_version);
  if (shown === null || !latestVersion) {
    return <EmptyState icon={FileText} title={t("noVersionTitle")} description={t("noVersionText")} />;
  }
  const at = (key: string, more: { edit?: boolean; v?: number | null } = {}) =>
    documentHref(project, slug, { v: more.v === undefined ? (asked.version === null ? null : shown) : more.v, s: key, edit: more.edit });
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(doc) => {
        const items = doc.shown?.items ?? [];
        const version = versionOf(latest, shown)!;
        const item = (asked.item ? findItem(items, asked.item) : null) ?? firstItem(items);
        const links: ItemLinks = { item: (ref) => itemHref(project, slug, ref, asked.version === null ? null : shown), section: (key) => at(key) };
        const editing = asked.edit && writer && item !== null;
        const actions = item ? (
          <>
            {writer && !editing ? (
              <Button asChild variant="secondary" size="sm">
                <Link href={at(item.key, { edit: true, v: latest.latest_version })} scroll={false} data-testid="srs-edit">
                  <Pencil aria-hidden="true" />
                  {isLatest ? t("edit") : t("editLatest", { version: latest.latest_version! })}
                </Link>
              </Button>
            ) : null}
            {item.code && CODED_KINDS.has(item.kind) ? (
              <Button asChild variant="secondary" size="sm">
                <Link href={links.item(item.code)} data-testid="srs-item-page">
                  <ArrowUpRight aria-hidden="true" />
                  {t("itemPage")}
                </Link>
              </Button>
            ) : null}
          </>
        ) : null;
        return (
          <div className="flex flex-col gap-6" data-testid="srs-document">
            <DocumentHead project={project} doc={latest} current="read" version={shown} />
            {viewer && !writer ? <ReaderNote project={project}>{t("readerText")}</ReaderNote> : null}
            {latestVersion.state === "draft" ? <DraftBanner project={project} doc={latest} version={latestVersion} writer={writer} /> : null}
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
              <VersionMenu versions={latest.versions} shown={shown} hrefOf={(v) => documentHref(project, slug, { v, s: item?.key })} />
              <p className="text-[13px] text-muted-foreground">
                {shown === approved ? t("showingApproved") : approved ? t("showingOther", { version: approved }) : t("noneApproved")}
              </p>
            </div>
            <div className="grid min-w-0 gap-6 lg:grid-cols-[15rem_minmax(0,1fr)] xl:grid-cols-[15rem_minmax(0,1fr)_18rem]">
              <div className="hidden lg:block">
                <div className="sticky top-16 max-h-[calc(100dvh-5rem)] overflow-y-auto pr-1">
                  <Toc items={items} selected={item?.key ?? null} changes={changes} hrefOf={(key) => at(key)} />
                </div>
              </div>
              <div className="flex min-w-0 flex-col gap-4">
                <div className="lg:hidden">
                  <TocDisclosure items={items} selected={item?.key ?? null} changes={changes} hrefOf={(key) => at(key)} />
                </div>
                {editing && item ? (
                  <ItemEditor
                    key={item.key}
                    project={project}
                    latest={latest}
                    itemKey={item.key}
                    cancelHref={at(item.key, { v: asked.version })}
                    onSaved={(number) => router.push(documentHref(project, slug, { v: number, s: item.key }), { scroll: false })}
                  />
                ) : null}
                {item ? (
                  <article className="flex min-w-0 flex-col gap-4 rounded-md border bg-card p-4 md:p-6" aria-labelledby="srs-item-title" data-testid="srs-item" data-item-key={item.key}>
                    <ItemHead item={item} change={changes.get(item.key)} actions={actions} />
                    <ItemBody items={items} item={item} links={links} />
                  </article>
                ) : (
                  <EmptyState icon={FileText} title={t("emptyTitle")} description={t("emptyText")} />
                )}
              </div>
              <div className="flex min-w-0 flex-col gap-4 lg:col-span-2 xl:col-span-1">
                <VersionCard project={project} doc={latest} version={version} />
                <ChecksCard problems={doc.shown?.problems ?? []} hrefOf={(key) => at(key)} />
                <CommentsCard project={project} slug={slug} />
              </div>
            </div>
          </div>
        );
      }}
    </QueryView>
  );
}

/**
 * A document's contents (/p/{project}/x/srs/documents/{slug}): the version approved last unless another is asked
 * (`v`), its headings as a tree beside the open item (`s`), each item drawn by its kind, a dot on what the version
 * changed since the approved one, and its facts, checks and open threads in the side column. A writer edits an item
 * of the latest version in place (`edit`), which saves the next version as a draft, and submits a draft for review.
 */
export function DocumentPage({ project, slug, asked, initialError }: { project: string; slug: string; asked: Asked; initialError: ApiErrorInfo | null }) {
  const state = useHubQuery(srsDocumentQuery(browserApi, project, slug, null), initialError);
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(latest) => <DocumentBody project={project} latest={latest} asked={asked} />}
    </QueryView>
  );
}
