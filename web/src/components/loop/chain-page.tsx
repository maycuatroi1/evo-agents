"use client";

import { Binoculars, Clock, IterationCw, MessageSquare, Signpost } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { RunRef } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { Banner, type BannerTone } from "@/components/feedback/banner";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { NotFoundState, PageSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { decideHref, LOOP_REFRESH_MS, type LoopItem, type LoopItemChain, type LoopItemEvent, loopHref, loopItemQuery } from "@/lib/loop-queries";

import { ItemStateBadge, KindTag, useItemTitle, useWaitingText } from "./badges";
import { ChainItemRow } from "./chain-list";
import { ChainRail, ChainRuns } from "./chain-rail";
import { useTries } from "./items-table";

/** A card with its h2 in a 48 px head over its content. */
function Card({ title, id, children, testId }: { title: string; id: string; children: ReactNode; testId: string }) {
  return (
    <section aria-labelledby={id} className="min-w-0 rounded-md border bg-card shadow-raised" data-testid={testId}>
      <div className="flex min-h-12 items-center border-b px-4 py-2">
        <h2 id={id} className="text-[15px] leading-[22px] font-semibold">
          {title}
        </h2>
      </div>
      {children}
    </section>
  );
}

const WAITING_TONE: Partial<Record<LoopItem["state"], { tone: BannerTone; icon: typeof Clock }>> = {
  queued: { tone: "neutral", icon: Clock },
  watching: { tone: "neutral", icon: Binoculars },
  waiting_human: { tone: "attention", icon: MessageSquare },
};

/** Why the work item waits, when it does: a person needed links to the Decisions page, where it is decided. */
function WaitingBanner({ project, item }: { project: string; item: LoopItem }) {
  const t = useTranslations("loop.chain");
  const waiting = useWaitingText();
  const look = WAITING_TONE[item.state];
  const why = waiting(item);
  if (!look || !why) return null;
  return (
    <Banner
      icon={look.icon}
      tone={look.tone}
      title={t(`waits.${item.state as "queued" | "watching" | "waiting_human"}`)}
      actions={
        item.state === "waiting_human" ? (
          <Button asChild variant="secondary" size="sm">
            <Link href={decideHref(project)} data-testid="chain-decide">
              <Signpost aria-hidden="true" />
              {t("decide")}
            </Link>
          </Button>
        ) : null
      }
      testId="chain-waiting"
    >
      {why}
    </Banner>
  );
}

/** The chain's work items, each under the one it came of, this one marked. */
function ChainItems({ project, found }: { project: string; found: LoopItemChain }) {
  const t = useTranslations("loop.chain");
  const parents = new Map(found.chain.map((item) => [item.id, item.parent]));
  const depth = (item: LoopItem) => {
    let at = item.parent;
    let count = 0;
    while (at !== null && at !== undefined && parents.has(at) && count < 8) {
      count += 1;
      at = parents.get(at) ?? null;
    }
    return count;
  };
  return (
    <Card title={t("itemsTitle", { count: found.chain.length })} id="chain-items-title" testId="chain-items">
      <ol className="divide-y">
        {found.chain.map((item) => (
          <ChainItemRow key={item.id} project={project} item={item} depth={depth(item)} current={item.id === found.item.id} />
        ))}
      </ol>
    </Card>
  );
}

function EventRow({ project, event }: { project: string; event: LoopItemEvent }) {
  const t = useTranslations("loop.chain");
  const kind = useTranslations("loop.event");
  const waiting = useWaitingText();
  const why = event.reason ? waiting({ reason: event.reason, waiting: event.figures, failures: 0, task: null }) : null;
  return (
    <li className="flex min-h-11 flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 text-[13px]" data-testid="chain-event" data-kind={event.kind}>
      <span className="w-36 shrink-0 text-xs text-fg-subtle">
        <When value={event.at} short />
      </span>
      <span className="font-medium text-foreground">{kind(event.kind)}</span>
      <ItemStateBadge state={event.state} />
      {why ? <span className="min-w-0 flex-1 truncate text-muted-foreground" title={why}>{why}</span> : null}
      {event.run !== null ? <RunRef id={event.run} href={`/p/${encodeURIComponent(project)}/runs/${event.run}` as Route} label={t("runLabel", { id: event.run })} /> : null}
    </li>
  );
}

function ChainBody({ project, found }: { project: string; found: LoopItemChain }) {
  const t = useTranslations("loop.chain");
  const cycle = useTranslations("loop.cycle");
  const title = useItemTitle();
  const tries = useTries();
  const { item } = found;
  const facts = [cycle(item.cycle), item.owner ? t("standsFor", { owner: item.owner }) : null, tries(item)].filter(Boolean).join(", ");
  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="chain-page" data-item-id={item.id}>
      <PageHeader
        title={title(item)}
        status={<ItemStateBadge state={item.state} size="lg" />}
        tags={
          <>
            <KindTag kind={item.kind} />
            <span className="font-mono text-[13px] text-fg-subtle tabular-nums">#{item.id}</span>
          </>
        }
        sub={
          <>
            <Link href={loopHref(project)} className="inline-flex items-center gap-1 text-brand hover:text-brand-hover">
              <IterationCw className="size-3.5" aria-hidden="true" />
              {t("back")}
            </Link>
            <span className="ml-2">{facts}</span>
          </>
        }
      />
      <WaitingBanner project={project} item={item} />
      <section aria-labelledby="chain-rail-title" className="flex min-w-0 flex-col gap-3">
        <h2 id="chain-rail-title" className="text-[15px] leading-[22px] font-semibold">
          {t("railTitle")}
        </h2>
        <ChainRail project={project} stages={found.stages} />
        <ChainRuns project={project} stages={found.stages} />
      </section>
      <div className="grid items-start gap-4 xl:grid-cols-2">
        <ChainItems project={project} found={found} />
        <Card title={t("historyTitle")} id="chain-history-title" testId="chain-history">
          {found.events.length === 0 ? (
            <p className="px-4 py-6 text-center text-[13px] text-muted-foreground">{t("noHistory")}</p>
          ) : (
            <ol className="divide-y">
              {found.events.map((event) => (
                <EventRow key={event.id} project={project} event={event} />
              ))}
            </ol>
          )}
        </Card>
      </div>
    </div>
  );
}

/**
 * One work item of a project's Loop with its chain (/p/{project}/loop/items/{id}), for any member with a grant on the
 * project, kept current while it is open: what it is and where it stands, why it waits, the stages of its chain from
 * the goal to the release, the chain's work items and what happened to this one. It only reads.
 */
export function ChainPage({ project, id, initialError }: { project: string; id: number; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("loop.chain");
  const state = useHubQuery({ ...loopItemQuery(browserApi, project, id), refetchInterval: LOOP_REFRESH_MS }, initialError, { live: true });
  if (state.status === "error" && state.error.status === 404) {
    return <NotFoundState title={t("notFoundTitle", { id })} description={t("notFoundText")} />;
  }
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(found) => <ChainBody project={project} found={found} />}
    </QueryView>
  );
}
