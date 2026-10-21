"use client";

import { useQuery } from "@tanstack/react-query";
import { BookOpen, FileDiff, Pencil, Plus } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { Banner } from "@/components/feedback/banner";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { NotFoundState, PageSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { usePluginPageOn } from "@/hooks/use-plugin-on";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { newTaskHref, TASKS_BOARD, TASKS_PLUGIN } from "@/lib/core-plugin-links";

import { type SrsDocument, type SrsItem, srsCommentsQuery, srsDiffQuery, srsDocumentQuery } from "./api";
import { CodeChip, KindTag, VersionBadge } from "./badges";
import { CommentForm, ThreadView } from "./comments";
import { rulesOf } from "./content-model";
import { useSrsViewer } from "./hooks";
import { ItemBody, type ItemLinks } from "./item-body";
import { compareHref, documentHref, itemHref } from "./links";
import { canWrite, CODED_KINDS, findItem, pathOf, shownNumber, threadsOf, versionOf } from "./model";
import { TraceCard } from "./trace-card";
import { VersionMenu } from "./version-menu";

const LATER = { added: "laterAdded", changed: "laterChanged", moved: "laterMoved", removed: "laterRemoved" } as const;

/** The threads about the item, and for a use case those about its business rules. */
function ItemComments({ project, slug, items, item, version, writer }: { project: string; slug: string; items: SrsItem[]; item: SrsItem; version: number; writer: boolean }) {
  const t = useTranslations("srs.itemPage");
  const { data } = useQuery({ ...srsCommentsQuery(browserApi, project, slug), retry: false });
  const keys = new Set([item.key, ...(item.kind === "use_case" ? rulesOf(items, item).map((rule) => rule.key) : [])]);
  const threads = threadsOf(data ?? []).filter((thread) => thread.root.item !== null && keys.has(thread.root.item));
  return (
    <section className="flex flex-col gap-3" aria-labelledby="srs-item-comments">
      <h2 id="srs-item-comments" className="text-[15px] leading-[22px] font-semibold">
        {t("comments")}
      </h2>
      {threads.length === 0 ? <p className="text-[13px] text-muted-foreground">{t("noComments")}</p> : null}
      {threads.map((thread) => (
        <ThreadView key={thread.root.id} project={project} slug={slug} thread={thread} writer={writer} />
      ))}
      {writer ? (
        <div className="rounded-md border bg-card p-4">
          <CommentForm
            project={project}
            slug={slug}
            target={{ item: item.key, version }}
            label={t("newComment", { name: item.code ?? item.title, version })}
            submitLabel={t("comment")}
          />
        </div>
      ) : null}
    </section>
  );
}

function ItemView({ project, latest, item, items, shown, asked }: { project: string; latest: SrsDocument; item: SrsItem; items: SrsItem[]; shown: number; asked: number | null }) {
  const t = useTranslations("srs.itemPage");
  const tState = useTranslations("srs.versionState");
  const viewer = useSrsViewer(project);
  const writer = canWrite(viewer);
  const tasksOn = usePluginPageOn(project, TASKS_PLUGIN, TASKS_BOARD);
  const slug = latest.slug;
  const newest = latest.latest_version;
  const diff = useQuery({ ...srsDiffQuery(browserApi, project, slug, shown, newest ?? shown), enabled: Boolean(newest && newest > shown), retry: false });
  const later = diff.data?.items.find((change) => change.key === item.key) ?? null;
  const version = versionOf(latest, shown);
  const newestVersion = versionOf(latest, newest);
  const path = pathOf(items, item.key).map((above) => above.code ?? above.title);
  const links: ItemLinks = {
    item: (ref) => itemHref(project, slug, ref, asked),
    section: (key) => documentHref(project, slug, { v: asked, s: key }),
  };
  return (
    <div className="flex flex-col gap-6" data-testid="srs-item-page" data-item-key={item.key}>
      <PageHeader
        leading={item.code ? <CodeChip code={item.code} testId="srs-item-code" /> : null}
        title={item.title}
        status={version ? <VersionBadge state={version.state} size="lg" /> : null}
        tags={<KindTag kind={item.kind} />}
        sub={[
          path.length ? `${t("in", { title: latest.title })}, ${t("under", { path: path.join(" / ") })}` : t("in", { title: latest.title }),
          item.aliases.length ? t("aliases", { aliases: item.aliases.join(", ") }) : null,
          t("showing", { version: shown }),
        ]
          .filter(Boolean)
          .join(". ")}
        actions={
          <>
            <Button asChild variant="secondary">
              <Link href={documentHref(project, slug, { v: asked, s: item.key })}>
                <BookOpen aria-hidden="true" />
                {t("inDocument")}
              </Link>
            </Button>
            {writer && newest ? (
              <Button asChild variant="secondary">
                <Link href={documentHref(project, slug, { v: newest, s: item.key, edit: true })} data-testid="srs-item-edit">
                  <Pencil aria-hidden="true" />
                  {t("edit")}
                </Link>
              </Button>
            ) : null}
            {writer && tasksOn && item.code && CODED_KINDS.has(item.kind) ? (
              <Button asChild variant="secondary">
                <Link href={newTaskHref(project, { link: item.code })} data-testid="srs-item-create-task">
                  <Plus aria-hidden="true" />
                  {t("createTask")}
                </Link>
              </Button>
            ) : null}
          </>
        }
        testId="srs-item-head"
      />
      <div className="flex flex-wrap items-center gap-3">
        <VersionMenu versions={latest.versions} shown={shown} hrefOf={(v) => itemHref(project, slug, item.code ?? item.key, v)} />
      </div>
      {later && newest && newestVersion ? (
        <Banner
          icon={FileDiff}
          tone="review"
          title={t(LATER[later.change], { version: newest, state: tState(newestVersion.state) })}
          actions={
            <>
              <Button asChild size="sm" variant="secondary">
                <Link href={itemHref(project, slug, item.code ?? item.key, newest)}>{t("showVersion", { version: newest })}</Link>
              </Button>
              <Button asChild size="sm" variant="secondary">
                <Link href={compareHref(project, slug, shown, newest)}>{t("compare")}</Link>
              </Button>
            </>
          }
          testId="srs-item-later"
        >
          {t("laterText", { version: shown })}
        </Banner>
      ) : null}
      <div className="grid min-w-0 gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="flex min-w-0 flex-col gap-6">
          <article className="min-w-0 rounded-md border bg-card p-4 md:p-6" aria-label={item.code ?? item.title}>
            <ItemBody items={items} item={item} links={links} />
          </article>
          <ItemComments project={project} slug={slug} items={items} item={item} version={shown} writer={writer} />
        </div>
        <div className="flex min-w-0 flex-col gap-4">
          <TraceCard project={project} item={item} tasksOn={tasksOn} />
        </div>
      </div>
    </div>
  );
}

function ItemAt({ project, latest, itemRef, asked }: { project: string; latest: SrsDocument; itemRef: string; asked: number | null }) {
  const t = useTranslations("srs.itemPage");
  const shown = shownNumber(latest, asked);
  const inShown = (doc: SrsDocument) => (doc.shown ? findItem(doc.shown.items, itemRef) : null);
  const state = useHubQuery(srsDocumentQuery(browserApi, project, latest.slug, shown === latest.latest_version ? null : shown));
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(doc) => {
        // Without `v`, an item new since the approved version (a draft's, or one in review) is read from the latest.
        const fromLatest = !inShown(doc) && asked === null ? inShown(latest) : null;
        const item = fromLatest ?? inShown(doc);
        const at = fromLatest ? latest.latest_version : shown;
        if (!item || at === null) {
          return <NotFoundState title={t("missingTitle", { ref: itemRef })} description={t("missingText", { version: at ?? 0 })} />;
        }
        const items = (fromLatest ? latest.shown?.items : doc.shown?.items) ?? [];
        return <ItemView project={project} latest={latest} item={item} items={items} shown={at} asked={asked} />;
      }}
    </QueryView>
  );
}

/**
 * One coded item (/p/{project}/x/srs/documents/{slug}/items/{ref}, by code, alias or key): a use case with its fields,
 * Activity Flow, business rules and screens, or a rule, screen or requirement, at the approved version unless `v`
 * asks another; a banner when a newer version changes it; its threads; and its trace in the knowledge graph.
 */
export function ItemPage({ project, slug, itemRef, asked, initialError }: { project: string; slug: string; itemRef: string; asked: number | null; initialError: ApiErrorInfo | null }) {
  const state = useHubQuery(srsDocumentQuery(browserApi, project, slug, null), initialError);
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(latest) => <ItemAt project={project} latest={latest} itemRef={itemRef} asked={asked} />}
    </QueryView>
  );
}
