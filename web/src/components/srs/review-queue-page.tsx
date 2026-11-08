"use client";

import { CircleCheck, Eye, FileText, Hourglass, ListChecks, Lock, Undo2 } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Ago } from "@/components/data/ago";
import { FacetGroup } from "@/components/data/facet-group";
import { NAME_LINK, Tag } from "@/components/data/identifier";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, ListSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { type SrsQueuedVersion, type SrsUseCase, srsReviewQueueQuery } from "./api";
import { CaseStateBadge, ChangeBadge, CodeChip, VersionChip } from "./badges";
import { ReviewRunRef } from "./findings-card";
import { useSrsViewer } from "./hooks";
import { caseReviewHref, documentHref, documentsHref, reviewHref } from "./links";
import { defaultFilter, mayReviewCase, QUEUE_REFRESH_MS, type QueueFilter, type QueueGroup, queueCounts, queueGroups } from "./review-queue-model";
import { caseName, caseRef } from "./use-case-model";

/** What holds the version's approval now: @ba's review of its round, open findings to decide, or nothing. */
function HoldLine({ project, version }: { project: string; version: SrsQueuedVersion }) {
  const t = useTranslations("srs.queue");
  const line = "flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] leading-[18px]";
  if (version.held_by === "ba_review") {
    return (
      <p className={`${line} text-muted-foreground`} data-testid="srs-queue-hold" data-hold="ba_review">
        <Hourglass className="size-4 shrink-0" aria-hidden="true" />
        {t("baReviewing")}
        {version.review_run ? <ReviewRunRef project={project} run={version.review_run} /> : null}
      </p>
    );
  }
  if (version.held_by === "findings") {
    return (
      <p className={`${line} font-medium text-attention`} data-testid="srs-queue-hold" data-hold="findings">
        <Lock className="size-4 shrink-0" aria-hidden="true" />
        {t("deciding", { count: version.deciding })}
      </p>
    );
  }
  return (
    <p className={`${line} text-muted-foreground`} data-testid="srs-queue-hold" data-hold="none">
      <Eye className="size-4 shrink-0" aria-hidden="true" />
      {t("ready")}
    </p>
  );
}

/** Who reviewed a use case, each with what they said: a check for an approval, a return arrow for changes asked. */
function Reviewers({ useCase, login }: { useCase: SrsUseCase; login: string | null }) {
  const t = useTranslations("srs.queue");
  const tReview = useTranslations("srs.review");
  if (useCase.reviews.length === 0) return <span className="text-xs text-fg-subtle">{t("noReview")}</span>;
  return (
    <ul className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground" aria-label={t("reviewersLabel", { name: caseName(useCase) })} role="list">
      {useCase.reviews.map((review) => {
        const approve = review.decision === "approve";
        const Icon = approve ? CircleCheck : Undo2;
        const said = tReview(`decisions.${review.decision}`);
        return (
          <li key={`${review.reviewer}-${review.created_at}`} className="inline-flex items-center gap-1" title={said} data-testid="srs-queue-reviewer" data-decision={review.decision}>
            <Icon className={`size-3.5 shrink-0 ${approve ? "text-success" : "text-attention"}`} aria-hidden="true" />
            <span className="font-medium text-foreground">{review.reviewer}</span>
            {review.reviewer === login ? <span className="text-fg-subtle">{t("you")}</span> : null}
            <span className="sr-only">{said}</span>
          </li>
        );
      })}
    </ul>
  );
}

/** One use case of a version in the queue: its code and title, linking to its row on the review page, where it stands, who reviewed it, and Review. */
function CaseRow({ project, version, useCase, login }: { project: string; version: SrsQueuedVersion; useCase: SrsUseCase; login: string | null }) {
  const t = useTranslations("srs.queue");
  const href = caseReviewHref(project, version.document, version.number, useCase.key);
  const name = caseName(useCase);
  return (
    <li className="flex flex-col gap-2 px-4 py-3 md:flex-row md:items-center md:gap-4" data-testid="srs-queue-case" data-key={useCase.key} data-state={useCase.state}>
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1">
        {useCase.code ? <CodeChip code={caseRef(useCase)} /> : null}
        <Link href={href} className={`${NAME_LINK} min-w-0 text-sm [overflow-wrap:anywhere]`} data-testid="srs-queue-case-link">
          {useCase.title}
        </Link>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <span className="flex flex-wrap items-center gap-2">
          <ChangeBadge change={useCase.change} />
          <CaseStateBadge state={useCase.state} />
        </span>
        <Reviewers useCase={useCase} login={login} />
        {mayReviewCase(version, useCase) ? (
          <Button asChild size="sm" variant="secondary">
            <Link href={href} aria-label={t("reviewFor", { name })} data-testid="srs-queue-review">
              {t("review")}
            </Link>
          </Button>
        ) : null}
      </div>
    </li>
  );
}

/** A version in review: its document and number, its author, when it was submitted and its round, what holds it, then its use cases. */
function VersionGroup({ project, group, login }: { project: string; group: QueueGroup; login: string | null }) {
  const t = useTranslations("srs.queue");
  const titleId = useId();
  const { version, cases } = group;
  const review = reviewHref(project, version.document, version.number);
  return (
    <Card
      size="sm"
      role="region"
      aria-labelledby={titleId}
      className="gap-0 py-0"
      data-testid="srs-queue-version"
      data-document={version.document}
      data-version={version.number}
      data-held-by={version.held_by ?? "none"}
    >
      <CardHeader className="gap-1.5 border-b py-3">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <h2 id={titleId} className="min-w-0 text-[15px] leading-[22px] font-semibold [overflow-wrap:anywhere]">
            <Link href={documentHref(project, version.document)} className={NAME_LINK}>
              {version.document_title}
            </Link>
          </h2>
          <VersionChip number={version.number} href={review} label={t("openReview", { version: version.number, title: version.document_title })} />
          {version.round > 1 ? <Tag data-testid="srs-queue-round">{t("round", { round: version.round })}</Tag> : null}
        </div>
        <p className="text-[13px] leading-[18px] text-muted-foreground" data-testid="srs-queue-byline">
          {t("by", { author: version.author })}
          {version.submitted_at ? (
            <>
              {", "}
              {t("submitted")} <Ago value={version.submitted_at} never="" />
            </>
          ) : null}
        </p>
        <HoldLine project={project} version={version} />
      </CardHeader>
      <CardContent className="px-0">
        {cases.length === 0 ? (
          <div className="flex flex-col gap-2 px-4 py-3 md:flex-row md:items-center md:gap-4">
            <p className="flex-1 text-[13px] text-muted-foreground" data-testid="srs-queue-no-cases">
              {t("noCases", { version: version.number })}
            </p>
            {version.may_review ? (
              <Button asChild size="sm" variant="secondary">
                <Link href={review} data-testid="srs-queue-review">
                  {t("reviewVersion", { version: version.number })}
                </Link>
              </Button>
            ) : null}
          </div>
        ) : (
          <ul className="flex flex-col divide-y" aria-label={t("casesLabel", { version: version.number, title: version.document_title })} role="list">
            {cases.map((useCase) => (
              <CaseRow key={useCase.key} project={project} version={version} useCase={useCase} login={login} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

/** The filter and the versions it shows, or why it shows none. */
function QueueList({
  project,
  queue,
  filter,
  onFilter,
  login,
}: {
  project: string;
  queue: SrsQueuedVersion[];
  filter: QueueFilter;
  onFilter: (filter: QueueFilter) => void;
  login: string | null;
}) {
  const t = useTranslations("srs.queue");
  if (queue.length === 0) {
    return <EmptyState icon={ListChecks} title={t("emptyTitle")} description={t("emptyText")} testId="srs-queue-empty" />;
  }
  const counts = queueCounts(queue, login);
  const groups = queueGroups(queue, filter, login);
  return (
    <div className="flex flex-col gap-4">
      <FacetGroup
        label={t("filterLabel")}
        options={[
          { value: "mine", label: t("filterMine"), count: counts.mine },
          { value: null, label: t("filterAll"), count: counts.all },
        ]}
        selected={filter === "mine" ? "mine" : null}
        onSelect={(value) => onFilter(value === "mine" ? "mine" : "all")}
        countLabel={(count) => t("toReview", { count })}
        testId="srs-queue-filter"
      />
      {groups.length === 0 ? (
        <EmptyState icon={CircleCheck} title={t("mineEmptyTitle")} description={t("mineEmptyText")} testId="srs-queue-mine-empty">
          <Button type="button" variant="secondary" onClick={() => onFilter("all")} data-testid="srs-queue-show-all">
            {t("showAll")}
          </Button>
        </EmptyState>
      ) : (
        <section className="flex flex-col gap-4" aria-label={t("listLabel")}>
          {groups.map((group) => (
            <VersionGroup key={`${group.version.document}-${group.version.number}`} project={project} group={group} login={login} />
          ))}
        </section>
      )}
    </div>
  );
}

/**
 * The review queue of a project (/p/{project}/x/srs/reviews, the page `reviews` of the core plugin srs): every version in
 * review of the documents the viewer sees, the one submitted first first, each under a heading with its document, number,
 * author, submission and round and a line saying what holds its approval (@ba's review of the round, open findings to
 * decide, or nothing), then its use cases with what the version does to each, where each stands, who reviewed it and,
 * for a reviewer who may review the version, Review, which opens the use case on the version's review page. "Waiting for
 * me" keeps the use cases still pending that the viewer may review and has not; it is where a reviewer lands. While @ba
 * reviews a version of the queue, the page reads it again, so the line changes when the review ends.
 */
export function ReviewQueuePage({ project, title, initialError }: { project: string; title: string; initialError: ApiErrorInfo | null }) {
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
  const [picked, setPicked] = useState<QueueFilter | null>(null);
  const queue = state.status === "success" ? state.data : null;
  const filter = picked ?? (queue ? defaultFilter(queue) : "all");
  const counts = queue ? queueCounts(queue, login) : null;
  return (
    <div className="flex flex-col gap-6" data-testid="srs-queue" data-filter={filter}>
      <PageHeader
        title={title}
        tags={
          counts ? (
            <>
              <Badge variant="secondary" data-testid="srs-queue-versions-count" data-count={counts.versions}>
                <ListChecks aria-hidden="true" />
                {t("versionsCount", { count: counts.versions })}
              </Badge>
              {counts.mine > 0 ? (
                <Badge variant="warning" data-testid="srs-queue-waiting-count" data-count={counts.mine}>
                  {t("waitingCount", { count: counts.mine })}
                </Badge>
              ) : null}
            </>
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
        {(data) => <QueueList project={project} queue={data} filter={filter} onFilter={setPicked} login={login} />}
      </QueryView>
    </div>
  );
}
