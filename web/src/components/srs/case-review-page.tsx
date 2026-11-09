"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { useEffect, useRef } from "react";

import { usePageCrumb } from "@/components/shell/page-crumb";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { NotFoundState, PageSkeleton } from "@/components/states/states";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { type SrsDocument, type SrsVersion, srsConfigQuery, srsDocumentQuery, srsOpenFindingsQuery, srsReviewQueueQuery } from "./api";
import { CaseChangesCard } from "./case-changes-card";
import { CaseContentCard } from "./case-content-card";
import { CaseDecisionCard } from "./case-decision-card";
import { CaseFindingsCard } from "./case-findings-card";
import { approveGate, caseAccess, caseFindings, type CaseView, keysUnder, shownCase } from "./case-review-model";
import { reviewHolds } from "./findings-model";
import { useSrsViewer } from "./hooks";
import { documentHref, itemHref, reviewsHref } from "./links";
import { canWrite, caseReviewBlock, findItem, selfApprovalOf, versionOf, waitForBaOf } from "./model";
import { nextCard, QUEUE_REFRESH_MS } from "./review-queue-model";
import { caseBlock } from "./use-case-model";

const STEPS = [
  { id: "srs-step-content", key: "content" },
  { id: "srs-step-changes", key: "changes" },
  { id: "srs-step-ba", key: "ba" },
  { id: "srs-step-decide", key: "decide" },
] as const;

/** The four steps of the review as links to their cards, so a first-time reviewer sees the way through at once. */
function StepStrip() {
  const t = useTranslations("srs.caseReview");
  return (
    <nav aria-label={t("stepsLabel")} data-testid="srs-case-steps">
      <ol className="grid grid-cols-2 gap-2 md:grid-cols-4">
        {STEPS.map((step) => (
          <li key={step.id} className="min-w-0">
            <a
              href={`#${step.id}`}
              className="flex min-h-11 items-center rounded-md border bg-card px-3 py-2 text-[13px] leading-[18px] font-medium transition-colors hover:bg-accent md:min-h-9"
            >
              {t(`step.${step.key}`)}
            </a>
          </li>
        ))}
      </ol>
    </nav>
  );
}

/** The four cards once the page knows the use case, the viewer and @ba's findings. */
function CaseBody({ view, caseKey }: { view: CaseView; caseKey: string }) {
  const t = useTranslations("srs.caseReview");
  const format = useFormatter();
  const { project, slug, latest, version, useCase } = view;
  const pathname = usePathname();
  const viewer = useSrsViewer(project);
  const login = viewer?.login ?? null;
  const config = useQuery({ ...srsConfigQuery(browserApi, project), retry: false });
  // While @ba checks the version, its findings are read again until it ends, and once more as it ends.
  const reviewing = reviewHolds(version.review_run);
  const findings = useQuery({ ...srsOpenFindingsQuery(browserApi, project, slug), retry: false, refetchInterval: reviewing ? QUEUE_REFRESH_MS : false });
  const { refetch: refetchFindings } = findings;
  const wasReviewing = useRef(reviewing);
  useEffect(() => {
    if (wasReviewing.current && !reviewing) void refetchFindings();
    wasReviewing.current = reviewing;
  }, [reviewing, refetchFindings]);
  const queue = useQuery({ ...srsReviewQueueQuery(browserApi, project), retry: false });

  const shown = shownCase(view)!;
  const keys = new Set([...keysUnder(view.items, shown.item.key), ...keysUnder(view.baseItems, shown.item.key)]);
  const groups = findings.data ? caseFindings(findings.data, keys) : null;
  // A use case sent to review on its own is reviewed in a version that is not: its gate is its own findings.
  const open = version.state === "in_review" || (useCase?.submitted ?? false);
  const fields = config.data?.fields ?? [];
  const waitForBa = waitForBaOf(fields);
  const gate = open ? approveGate(version.state === "in_review" ? version.review_run : null, groups, waitForBa) : null;
  const block = viewer ? caseReviewBlock(viewer, version, useCase, selfApprovalOf(fields)) : null;
  const access = caseAccess({ signedIn: viewer !== null, block: caseBlock(block), configPending: config.isPending, toReview: useCase !== null });
  const next = queue.data ? nextCard(queue.data, login, { document: slug, number: version.number, key: useCase?.key ?? caseKey }) : null;

  const day = (value: string) => format.dateTime(new Date(value), { dateStyle: "medium" });
  const bare = shown.item.code ?? useCase?.code ?? shown.item.key;
  const code = useCase?.rev ? `${bare} r${useCase.rev}` : bare;
  usePageCrumb(pathname, code);
  const sent = { code, author: version.author, date: day(version.submitted_at ?? version.created_at) };
  const inUse = latest.approved_version === null ? null : versionOf(latest, latest.approved_version);
  const sub = [
    access === "review" ? t("subAsks", sent) : t("subSent", sent),
    inUse ? (inUse.decided_at ? t("inUse", { date: day(inUse.decided_at) }) : t("inUseVersion")) : t("firstOne"),
  ].join(" ");

  return (
    <div className="flex flex-col gap-6" data-testid="srs-case-review" data-key={shown.item.key} data-access={access}>
      <Link href={reviewsHref(project)} className="inline-flex items-center gap-1.5 self-start text-[13px] text-muted-foreground hover:text-foreground" data-testid="srs-case-back">
        <ArrowLeft className="size-4" aria-hidden="true" />
        {t("back")}
      </Link>
      <PageHeader title={shown.item.title} sub={sub} testId="srs-case-head" />
      <StepStrip />
      <div className="grid min-w-0 gap-6 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <div className="flex min-w-0 flex-col gap-6">
          <CaseContentCard view={view} />
          <CaseChangesCard view={view} />
        </div>
        <div className="flex min-w-0 flex-col gap-6">
          <CaseFindingsCard
            project={project}
            slug={slug}
            caseKey={shown.item.key}
            run={version.state === "in_review" ? version.review_run : null}
            findings={findings}
            groups={groups}
            writer={canWrite(viewer)}
            waitForBa={waitForBa}
          />
          <CaseDecisionCard
            project={project}
            slug={slug}
            number={version.number}
            useCase={useCase}
            author={version.author}
            access={access}
            gate={gate}
            login={login}
            next={next}
          />
        </div>
      </div>
    </div>
  );
}

/** Reads the version under review and the version approved last, then finds the use case in either. */
function CaseAt({ project, latest, version, caseKey }: { project: string; latest: SrsDocument; version: SrsVersion; caseKey: string }) {
  const t = useTranslations("srs.caseReview");
  const slug = latest.slug;
  const approved = latest.approved_version !== null && latest.approved_version < version.number ? latest.approved_version : null;
  const state = useHubQuery(srsDocumentQuery(browserApi, project, slug, version.number === latest.latest_version ? null : version.number));
  const base = useQuery({ ...srsDocumentQuery(browserApi, project, slug, approved ?? 0), enabled: approved !== null, retry: false });
  if (approved !== null && base.isPending) return <PageSkeleton />;
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(doc) => {
        const items = doc.shown?.items ?? [];
        const baseItems = base.data?.shown?.items ?? [];
        const asUseCase = (found: ReturnType<typeof findItem>) => (found?.kind === "use_case" ? found : null);
        const item = asUseCase(findItem(items, caseKey));
        const before = asUseCase(findItem(baseItems, item?.key ?? caseKey));
        if (!item && !before) return <NotFoundState title={t("missingTitle")} description={t("missingText", { ref: caseKey })} />;
        const key = (item ?? before)!.key;
        const view: CaseView = {
          project,
          slug,
          latest,
          version,
          items,
          item,
          approved,
          compared: approved === null || base.isSuccess,
          baseItems,
          base: before,
          useCase: doc.shown?.use_cases.find((one) => one.key === key) ?? null,
          links: {
            item: (ref) => itemHref(project, slug, ref, item ? version.number : approved),
            section: (at) => documentHref(project, slug, { v: item ? version.number : approved, s: at }),
          },
        };
        return <CaseBody view={view} caseKey={caseKey} />;
      }}
    </QueryView>
  );
}

/**
 * The review of one use case of a version (/p/{project}/x/srs/documents/{slug}/versions/{number}/cases/{key}), written
 * for a reviewer who has never used the hub: back to the queue, the use case's title, who asks for the review and
 * which version is in use, the four steps, then a card per step: what the use case says (its changes marked), what
 * changed this time, what @ba noted (points to settle by picking a choice), and the decision. While @ba checks the
 * version, the page reads it again every 15 s.
 */
export function CaseReviewPage({ project, slug, number, caseKey, initialError }: { project: string; slug: string; number: number; caseKey: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("srs.caseReview");
  const state = useHubQuery(
    {
      ...srsDocumentQuery(browserApi, project, slug, null),
      refetchInterval: (held) => (reviewHolds(held.state.data && versionOf(held.state.data, number)?.review_run) ? QUEUE_REFRESH_MS : false),
    },
    initialError,
  );
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(latest) => {
        const version = versionOf(latest, number);
        if (!version) return <NotFoundState title={t("missingVersionTitle")} description={t("missingVersionText", { title: latest.title })} />;
        return <CaseAt project={project} latest={latest} version={version} caseKey={caseKey} />;
      }}
    </QueryView>
  );
}
