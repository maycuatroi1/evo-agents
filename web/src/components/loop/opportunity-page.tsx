"use client";

import { CircleCheck, CircleMinus, CircleX, Eye, PencilLine, RotateCcw, Telescope } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Fact, Facts } from "@/components/data/facts";
import { Identifier } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { Banner } from "@/components/feedback/banner";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { NotFoundState, PageSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { opportunitiesHref, opportunityQuery, type ProjectOpportunityShown } from "@/lib/opportunity-queries";

import { CloseOpportunityDialog, ReopenOpportunityDialog } from "./opportunity-close";
import { EditOpportunityDialog } from "./opportunity-dialog";
import { useOpportunityViewer } from "./opportunity-hooks";
import { Panel, WindowText } from "./opportunity-parts";
import { AnswersSection, EvidenceSection, GoalsSection, LoopSection, RevisionsCard } from "./opportunity-sections";
import { OpportunityStatusBadge } from "./opportunity-status";

type Open = "edit" | "close" | "reopen" | null;

const CLOSED_ICONS = { captured: CircleCheck, missed: CircleX, dropped: CircleMinus } as const;
const CLOSED_TONES = { captured: "success", missed: "attention", dropped: "neutral" } as const;

/** A closed opportunity says how it closed, why, and who closed it, above everything else on its page. */
function ClosedBanner({ opportunity }: { opportunity: ProjectOpportunityShown }) {
  const t = useTranslations("opportunities.page");
  if (opportunity.status === "open") return null;
  const status = opportunity.status;
  return (
    <Banner icon={CLOSED_ICONS[status]} tone={CLOSED_TONES[status]} title={t(`${status}Title`)} testId="opportunity-closed">
      <p className="text-foreground">{opportunity.close_reason}</p>
      <p>
        {t("closedBy", { by: opportunity.created_by })} <When value={opportunity.created_at} />
      </p>
    </Banner>
  );
}

/** What could be won, who gains, what makes it reachable now and when the window closes. */
function WhatCouldBeWon({ opportunity }: { opportunity: ProjectOpportunityShown }) {
  const t = useTranslations("opportunities.page");
  return (
    <Panel id="opportunity-won-title" title={t("wonTitle")} testId="opportunity-won">
      <Facts>
        <Fact label={t("beneficiary")} testId="opportunity-beneficiary">
          {opportunity.beneficiary}
        </Fact>
        <Fact label={t("leverage")} testId="opportunity-leverage">
          {opportunity.leverage}
        </Fact>
        <Fact label={t("window")} testId="opportunity-window">
          <WindowText windowUntil={opportunity.window_until} windowNote={opportunity.window_note} />
        </Fact>
      </Facts>
    </Panel>
  );
}

function DetailsCard({ opportunity }: { opportunity: ProjectOpportunityShown }) {
  const t = useTranslations("opportunities.page");
  return (
    <Panel id="opportunity-details-title" title={t("detailsTitle")} testId="opportunity-details">
      <Facts>
        <Fact label={t("owner")} testId="opportunity-owner">
          <span className="font-mono text-[13px]">{opportunity.owner}</span>
        </Fact>
        <Fact label={t("rank")} testId="opportunity-rank">
          <span className="tabular-nums">{opportunity.rank}</span>
        </Fact>
        <Fact label={t("revision")} testId="opportunity-revision">
          {t("revisionText", { revision: opportunity.revision, by: opportunity.created_by })}
        </Fact>
        <Fact label={t("changed")}>
          <When value={opportunity.created_at} />
        </Fact>
        {opportunity.metric ? (
          <Fact label={t("metric")}>
            <span className="font-mono text-xs">{opportunity.metric.key}</span>
          </Fact>
        ) : null}
        {opportunity.close_reason ? (
          <Fact label={t("closeReason")} testId="opportunity-close-reason">
            {opportunity.close_reason}
          </Fact>
        ) : null}
        {opportunity.note ? <Fact label={t("note")}>{opportunity.note}</Fact> : null}
      </Facts>
    </Panel>
  );
}

function OpportunityView({ project, opportunity }: { project: string; opportunity: ProjectOpportunityShown }) {
  const t = useTranslations("opportunities.page");
  const viewer = useOpportunityViewer(project);
  const writer = viewer?.writer ?? false;
  const [open, setOpen] = useState<Open>(null);
  const opened = (which: Exclude<Open, null>) => (next: boolean) => setOpen(next ? which : null);
  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="opportunity-page" data-opportunity={opportunity.code} data-revision={opportunity.revision}>
      <PageHeader
        leading={<Identifier value={opportunity.code} testId="opportunity-code" />}
        title={opportunity.statement}
        status={<OpportunityStatusBadge status={opportunity.status} size="lg" />}
        sub={
          <Link href={opportunitiesHref(project)} className="inline-flex items-center gap-1 text-brand hover:text-brand-hover" data-testid="opportunity-back">
            <Telescope className="size-3.5" aria-hidden="true" />
            {t("back")}
          </Link>
        }
        actions={
          writer ? (
            <>
              {opportunity.status === "open" ? (
                <Button type="button" variant="outline" onClick={() => setOpen("close")} data-testid="opportunity-close">
                  <CircleCheck aria-hidden="true" />
                  {t("close")}
                </Button>
              ) : (
                <Button type="button" variant="outline" onClick={() => setOpen("reopen")} data-testid="opportunity-reopen">
                  <RotateCcw aria-hidden="true" />
                  {t("reopen")}
                </Button>
              )}
              <Button type="button" onClick={() => setOpen("edit")} data-testid="opportunity-edit">
                <PencilLine aria-hidden="true" />
                {t("edit")}
              </Button>
            </>
          ) : null
        }
        testId="opportunity-head"
      />
      {viewer && !writer ? (
        <Banner icon={Eye} title={t("readerTitle")} testId="opportunity-read-only">
          {t("readerText")}
        </Banner>
      ) : null}
      <ClosedBanner opportunity={opportunity} />
      <div className="grid min-w-0 items-start gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="flex min-w-0 flex-col gap-6">
          <WhatCouldBeWon opportunity={opportunity} />
          <EvidenceSection project={project} opportunity={opportunity} />
          <AnswersSection project={project} opportunity={opportunity} />
          <GoalsSection project={project} code={opportunity.code} />
          <LoopSection project={project} code={opportunity.code} />
        </div>
        <div className="flex min-w-0 flex-col gap-6">
          <DetailsCard opportunity={opportunity} />
          <RevisionsCard project={project} code={opportunity.code} />
        </div>
      </div>
      {writer ? (
        <>
          <EditOpportunityDialog project={project} opportunity={opportunity} open={open === "edit"} onOpenChange={opened("edit")} />
          {opportunity.status === "open" ? (
            <CloseOpportunityDialog project={project} opportunity={opportunity} open={open === "close"} onOpenChange={opened("close")} />
          ) : (
            <ReopenOpportunityDialog project={project} opportunity={opportunity} open={open === "reopen"} onOpenChange={opened("reopen")} />
          )}
        </>
      ) : null}
    </div>
  );
}

/**
 * One opportunity (/p/{project}/opportunities/{code}), for any member with a grant on the project: what could be won,
 * who gains, what makes it reachable now and when the window closes, the evidence by signal, the problems it answers
 * (a count for those of projects the reader holds no grant on), the goals that answer it and the Loop's chains that go
 * from it; beside them its owner, rank and revisions. A writer edits it whole, closes it as captured, missed or
 * dropped with a reason, and reopens it; a reader reads.
 */
export function OpportunityPage({ project, code, initialError }: { project: string; code: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("opportunities.page");
  const state = useHubQuery(opportunityQuery(browserApi, project, code), initialError);
  if (state.status === "error" && state.error.status === 404) {
    return <NotFoundState title={t("notFoundTitle", { code })} description={t("notFoundText")} />;
  }
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(opportunity) => <OpportunityView project={project} opportunity={opportunity} />}
    </QueryView>
  );
}
