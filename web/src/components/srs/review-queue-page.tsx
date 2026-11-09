"use client";

import { CircleCheck, FileText } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type ReactNode, useId } from "react";

import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, ListSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { type SrsQueuedVersion, srsReviewQueueQuery } from "./api";
import { useSrsViewer } from "./hooks";
import { documentsHref } from "./links";
import { QueueCardView } from "./queue-card";
import { QUEUE_REFRESH_MS, type QueueCard, queueCards } from "./review-queue-model";

/** A group of cards under its heading and count. */
function Group({ title, count, testId, children }: { title: string; count: number; testId: string; children: ReactNode }) {
  const id = useId();
  return (
    <section aria-labelledby={id} className="flex flex-col gap-3" data-testid={testId} data-count={count}>
      <h2 id={id} className="flex items-center gap-2 text-[15px] leading-[22px] font-semibold">
        {title}
        <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-muted px-1.5 text-xs font-medium text-muted-foreground tabular-nums">
          {count}
        </span>
      </h2>
      {children}
    </section>
  );
}

function Cards({ project, cards, primary }: { project: string; cards: QueueCard[]; primary: boolean }) {
  return (
    <div className="grid gap-3 xl:grid-cols-2">
      {cards.map((card) => (
        <QueueCardView key={`${card.version.document}-${card.version.number}-${card.useCase?.key ?? ""}`} project={project} card={card} primary={primary} />
      ))}
    </div>
  );
}

/** The cards waiting for the viewer, then those waiting for @ba or others; a friendly line when none waits for them. */
function QueueList({ project, queue, login }: { project: string; queue: SrsQueuedVersion[]; login: string | null }) {
  const t = useTranslations("srs.queue");
  const { mine, others } = queueCards(queue, login);
  if (mine.length === 0 && others.length === 0) {
    return <EmptyState icon={CircleCheck} title={t("emptyTitle")} testId="srs-queue-empty" />;
  }
  return (
    <div className="flex flex-col gap-8">
      <Group title={t("mineTitle")} count={mine.length} testId="srs-queue-mine">
        {mine.length === 0 ? (
          <div className="flex items-start gap-2.5 rounded-md bg-surface-sunken px-4 py-3 text-[13px] leading-[18px]" data-testid="srs-queue-mine-empty">
            <CircleCheck className="mt-px size-4 shrink-0 text-success" aria-hidden="true" />
            <p className="min-w-0 font-medium text-foreground">{t("emptyTitle")}</p>
          </div>
        ) : (
          <Cards project={project} cards={mine} primary />
        )}
      </Group>
      {others.length > 0 ? (
        <Group title={t("othersTitle")} count={others.length} testId="srs-queue-others">
          <Cards project={project} cards={others} primary={false} />
        </Group>
      ) : null}
    </div>
  );
}

/**
 * The review queue of a project (/p/{project}/x/srs/reviews, the page `reviews` of the core plugin srs), written for a
 * reviewer who has never used the hub: one card per use case waiting for review, those waiting for the viewer first,
 * then those waiting for @ba or someone else. A version that changes no use case is one card. While @ba checks a version of the queue, the page reads it again, so the cards change when it
 * ends.
 */
export function ReviewQueuePage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("srs.queue");
  const viewer = useSrsViewer(project);
  const login = viewer?.login ?? null;
  const state = useHubQuery(
    {
      ...srsReviewQueueQuery(browserApi, project),
      refetchInterval: (query) => (query.state.data?.some((version) => version.held_by === "ba_review") ? QUEUE_REFRESH_MS : false),
    },
    initialError,
  );
  const waiting = state.status === "success" ? queueCards(state.data, login).mine.length : 0;
  return (
    <div className="flex flex-col gap-6" data-testid="srs-queue">
      <PageHeader
        title={t("title")}
        tags={
          waiting > 0 ? (
            <Badge variant="warning" data-testid="srs-queue-waiting-count" data-count={waiting}>
              {t("waitingCount", { count: waiting })}
            </Badge>
          ) : null
        }
        actions={
          <Button asChild variant="ghost">
            <Link href={documentsHref(project)} data-testid="srs-queue-documents">
              <FileText aria-hidden="true" />
              {t("documents")}
            </Link>
          </Button>
        }
      />
      <QueryView state={state} loading={<ListSkeleton rows={4} />}>
        {(data) => <QueueList project={project} queue={data} login={login} />}
      </QueryView>
    </div>
  );
}
