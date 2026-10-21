"use client";

import { useQuery } from "@tanstack/react-query";
import { Bot, CircleCheck, CircleMinus, FileDiff, Lock } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useEffect, useRef } from "react";

import { When } from "@/components/data/when";
import { Banner } from "@/components/feedback/banner";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { ListSkeleton, NotFoundState, PageSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { type SrsDocument, type SrsVersion, srsConfigQuery, srsDiffQuery, srsDocumentQuery, srsFindingsQuery } from "./api";
import { ChangeBadge, CodeChip, VersionBadge } from "./badges";
import { DocumentHead } from "./document-head";
import { FindingsCard } from "./findings-card";
import { approveLock, REVIEW_REFRESH_MS, reviewHolds } from "./findings-model";
import { useSrsViewer } from "./hooks";
import { compareHref, documentHref, itemHref } from "./links";
import { policyOf, reviewBlock, rolesOf, selfApprovalOf, slots, versionOf } from "./model";
import { ApprovalsCard, ReviewHistory } from "./review-parts";
import { ReviewForm } from "./review-form";
import { DraftBanner } from "./submit-banner";
import { ChecksCard } from "./version-rail";

/** What approving the version would approve: every item changed since the version approved last. */
function WhatChanges({ project, doc, version }: { project: string; doc: SrsDocument; version: SrsVersion }) {
  const t = useTranslations("srs.review");
  const approved = doc.approved_version;
  const since = approved !== null && approved < version.number ? approved : null;
  const diff = useQuery({ ...srsDiffQuery(browserApi, project, doc.slug, since ?? 0, version.number), enabled: since !== null, retry: false });
  return (
    <Card size="sm" data-testid="srs-review-changes">
      <CardHeader>
        <CardTitle>
          <h2>{t("changesTitle")}</h2>
        </CardTitle>
        <p className="text-xs text-fg-subtle">{since ? t("changesSince", { version: since }) : t("changesFirst")}</p>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {since === null ? null : diff.isPending ? (
          <ListSkeleton rows={3} />
        ) : diff.isError ? (
          <p className="text-[13px] text-danger">{t("changesFailed")}</p>
        ) : diff.data.items.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">{t("noChanges")}</p>
        ) : (
          <ul className="flex flex-col gap-2" role="list">
            {diff.data.items.map((item) => (
              <li key={item.key} className="flex min-w-0 flex-wrap items-center gap-2 text-[13px]">
                <ChangeBadge change={item.change} />
                {item.code ? <CodeChip code={item.code} href={item.change === "removed" ? undefined : itemHref(project, doc.slug, item.code, version.number)} /> : null}
                <span className="min-w-0 [overflow-wrap:anywhere]">{item.title_to ?? item.title_from}</span>
              </li>
            ))}
          </ul>
        )}
        {since ? (
          <Link href={compareHref(project, doc.slug, since, version.number)} className="inline-flex items-center gap-1.5 text-[13px] text-brand hover:text-brand-hover">
            <FileDiff className="size-4" aria-hidden="true" />
            {t("compareLink", { from: since, to: version.number })}
          </Link>
        ) : null}
      </CardContent>
    </Card>
  );
}

function ReviewBody({ project, latest, version }: { project: string; latest: SrsDocument; version: SrsVersion }) {
  const t = useTranslations("srs.review");
  const tState = useTranslations("srs.versionState");
  const viewer = useSrsViewer(project);
  const config = useQuery({ ...srsConfigQuery(browserApi, project), retry: false });
  const detail = useHubQuery(srsDocumentQuery(browserApi, project, latest.slug, version.number === latest.latest_version ? null : version.number));
  // @ba's review of the round holds approval while it is queued or running: the findings are read again until it ends,
  // and once more as it ends, for what it recorded after the last read.
  const reviewing = reviewHolds(version.review_run);
  const findings = useQuery({ ...srsFindingsQuery(browserApi, project, latest.slug), retry: false, refetchInterval: reviewing ? REVIEW_REFRESH_MS : false });
  const { refetch: refetchFindings } = findings;
  const wasReviewing = useRef(reviewing);
  useEffect(() => {
    if (wasReviewing.current && !reviewing) void refetchFindings();
    wasReviewing.current = reviewing;
  }, [reviewing, refetchFindings]);
  const lock = version.state === "in_review" ? approveLock(findings.data, version.review_run) : null;
  const fields = config.data?.fields ?? [];
  const policy = policyOf(fields, latest.kind);
  const selfApproval = selfApprovalOf(fields);
  const block = viewer ? reviewBlock(viewer, latest, version, selfApproval) : "reader";
  // An author waits for the config before being told they cannot review: self_approval may let them.
  const awaitingConfig = block === "author" && config.isPending;
  const approved = latest.approved_version;
  const written = viewer
    ? latest.versions
        .filter((v) => v.author === viewer.login && v.number > (approved ?? 0) && v.number <= version.number)
        .map((v) => v.number)
        .sort((a, b) => a - b)
        .map((number) => `v${number}`)
    : [];
  const reason =
    block === "reader"
      ? t("blocked.reader", { project })
      : block === "not_in_review"
        ? t("blocked.notInReview", { version: version.number, state: tState(version.state) })
        : block === "author"
          ? t("blocked.author", { versions: written.join(", "), since: approved ? `v${approved}` : t("firstVersion"), version: version.number })
          : t("blocked.reviewed", { version: version.number });
  return (
    <div className="flex flex-col gap-6" data-testid="srs-review" data-version={version.number}>
      <DocumentHead
        project={project}
        doc={latest}
        current="review"
        version={version.number}
        title={t("title", { version: version.number })}
        status={<VersionBadge state={version.state} size="lg" />}
        actions={
          approved && approved !== version.number ? (
            <Button asChild variant="secondary">
              <Link href={compareHref(project, latest.slug, Math.min(approved, version.number), Math.max(approved, version.number))}>
                <FileDiff aria-hidden="true" />
                {t("compareWith", { version: approved })}
              </Link>
            </Button>
          ) : null
        }
      />
      <p className="text-[13px] text-muted-foreground" data-testid="srs-review-sub">
        {[
          version.summary || t("noSummary"),
          t("by", { author: version.author }),
          approved && approved < version.number ? t("approvesSince", { version: version.number, approved }) : null,
        ]
          .filter(Boolean)
          .join(". ")}
      </p>
      {version.state === "approved" ? (
        <Banner icon={CircleCheck} tone="success" title={t("approvedTitle", { version: version.number })} testId="srs-review-approved">
          {t("approvedText")}
        </Banner>
      ) : null}
      {version.state === "superseded" ? (
        <Banner icon={CircleMinus} title={t("supersededTitle", { version: version.number })}>
          <Link href={documentHref(project, latest.slug)} className="text-brand underline underline-offset-4 hover:text-brand-hover">
            {t("openApproved")}
          </Link>
        </Banner>
      ) : null}
      {version.state === "draft" && version.number === latest.latest_version ? (
        <DraftBanner project={project} doc={latest} version={version} writer={block !== "reader"} />
      ) : null}
      <div className="grid min-w-0 gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="flex min-w-0 flex-col gap-6">
          <ApprovalsCard project={project} policy={policy} slots={slots(policy, version)} version={version} approved={approved} selfApproval={selfApproval} />
          <FindingsCard
            project={project}
            slug={latest.slug}
            round={version.round}
            run={version.review_run}
            inReview={version.state === "in_review"}
            findings={findings}
          />
          {version.state === "in_review" ? (
            awaitingConfig ? (
              <ListSkeleton rows={1} />
            ) : block === null && viewer ? (
              <div className="rounded-md border bg-card p-4">
                <ReviewForm project={project} slug={latest.slug} version={version} roles={rolesOf(fields, viewer.login)} lock={lock} />
              </div>
            ) : (
              <Banner icon={Lock} title={t("cannotTitle", { version: version.number })} testId="srs-review-blocked">
                {reason}
              </Banner>
            )
          ) : null}
          <Banner icon={Bot} title={t("agentsTitle")}>
            {t("agentsText")}
          </Banner>
          <ReviewHistory version={version} />
        </div>
        <div className="flex min-w-0 flex-col gap-4">
          <WhatChanges project={project} doc={latest} version={version} />
          <QueryView state={detail} loading={<ListSkeleton rows={2} />}>
            {(doc) => <ChecksCard problems={doc.shown?.problems ?? []} hrefOf={(key) => documentHref(project, latest.slug, { v: version.number, s: key })} />}
          </QueryView>
          <p className="text-xs text-fg-subtle">
            {t("saved")} <When value={version.created_at} />
          </p>
        </div>
      </div>
    </div>
  );
}

/**
 * A version's review (/p/{project}/x/srs/documents/{slug}/versions/{number}): the approvals the policy asks for and who
 * gave them this round, the conflicts @ba found in the document (the Findings card, which readers see too), the
 * reviewer's form for a writer who may review it (nobody who wrote a version since the one approved last, admins
 * included, unless the project turned `self_approval` on), why another may not, what approving it approves, its
 * checks and its history. While @ba's review of the version's round is queued or running, the document is read again
 * every few seconds, so the page sees it end.
 */
export function ReviewPage({ project, slug, number, initialError }: { project: string; slug: string; number: number; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("srs.review");
  const query = srsDocumentQuery(browserApi, project, slug, null);
  const state = useHubQuery(
    { ...query, refetchInterval: (held) => (reviewHolds(held.state.data && versionOf(held.state.data, number)?.review_run) ? REVIEW_REFRESH_MS : false) },
    initialError,
  );
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(latest) => {
        const version = versionOf(latest, number);
        if (!version) return <NotFoundState title={t("missingTitle", { version: number })} description={t("missingText", { title: latest.title })} />;
        return <ReviewBody project={project} latest={latest} version={version} />;
      }}
    </QueryView>
  );
}
