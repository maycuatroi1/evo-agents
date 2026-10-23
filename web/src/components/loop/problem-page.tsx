"use client";

import { Bandage, CircleCheck, CircleMinus, Eye, PencilLine, RotateCcw } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Identifier } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { Banner } from "@/components/feedback/banner";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { NotFoundState, PageSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { problemQuery, problemsHref, type ProjectProblemShown } from "@/lib/problem-queries";

import { CloseProblemDialog, ReopenProblemDialog } from "./problem-close";
import { EditProblemDialog } from "./problem-dialog";
import { useProblemViewer } from "./problem-hooks";
import { ClaimsSection, GoalsSection, LoopSection, PlansSection } from "./problem-links";
import { DetailsCard, MetricCard, RevisionsCard, WhatHurts } from "./problem-parts";
import { ProblemStatusBadge } from "./problem-status";

type Open = "edit" | "close" | "reopen" | null;

/** A closed problem says how it closed, why, and who closed it, above everything else on its page. */
function ClosedBanner({ problem }: { problem: ProjectProblemShown }) {
  const t = useTranslations("problems.page");
  if (problem.status === "open") return null;
  const solved = problem.status === "solved";
  return (
    <Banner
      icon={solved ? CircleCheck : CircleMinus}
      tone={solved ? "success" : "neutral"}
      title={t(solved ? "solvedTitle" : "droppedTitle")}
      testId="problem-closed"
    >
      <p className="text-foreground">{problem.close_reason}</p>
      <p>
        {t("closedBy", { by: problem.created_by })} <When value={problem.created_at} />
      </p>
    </Banner>
  );
}

function ProblemView({ project, problem }: { project: string; problem: ProjectProblemShown }) {
  const t = useTranslations("problems.page");
  const viewer = useProblemViewer(project);
  const writer = viewer?.writer ?? false;
  const [open, setOpen] = useState<Open>(null);
  const opened = (which: Exclude<Open, null>) => (next: boolean) => setOpen(next ? which : null);
  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="problem-page" data-problem={problem.code} data-revision={problem.revision}>
      <PageHeader
        leading={<Identifier value={problem.code} testId="problem-code" />}
        title={problem.statement}
        status={<ProblemStatusBadge status={problem.status} size="lg" />}
        sub={
          <Link href={problemsHref(project)} className="inline-flex items-center gap-1 text-brand hover:text-brand-hover" data-testid="problem-back">
            <Bandage className="size-3.5" aria-hidden="true" />
            {t("back")}
          </Link>
        }
        actions={
          writer ? (
            <>
              {problem.status === "open" ? (
                <Button type="button" variant="outline" onClick={() => setOpen("close")} data-testid="problem-close">
                  <CircleCheck aria-hidden="true" />
                  {t("close")}
                </Button>
              ) : (
                <Button type="button" variant="outline" onClick={() => setOpen("reopen")} data-testid="problem-reopen">
                  <RotateCcw aria-hidden="true" />
                  {t("reopen")}
                </Button>
              )}
              <Button type="button" onClick={() => setOpen("edit")} data-testid="problem-edit">
                <PencilLine aria-hidden="true" />
                {t("edit")}
              </Button>
            </>
          ) : null
        }
        testId="problem-head"
      />
      {viewer && !writer ? (
        <Banner icon={Eye} title={t("readerTitle")} testId="problem-read-only">
          {t("readerText")}
        </Banner>
      ) : null}
      <ClosedBanner problem={problem} />
      <div className="grid min-w-0 items-start gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="flex min-w-0 flex-col gap-6">
          <WhatHurts problem={problem} />
          <MetricCard project={project} problem={problem} />
          <GoalsSection project={project} goals={problem.goals} />
          <ClaimsSection project={project} claims={problem.claims} />
          <PlansSection project={project} plans={problem.plans} />
          <LoopSection project={project} problem={problem} />
        </div>
        <div className="flex min-w-0 flex-col gap-6">
          <DetailsCard problem={problem} />
          <RevisionsCard project={project} problem={problem} />
        </div>
      </div>
      {writer ? (
        <>
          <EditProblemDialog project={project} problem={problem} open={open === "edit"} onOpenChange={opened("edit")} />
          {problem.status === "open" ? (
            <CloseProblemDialog project={project} problem={problem} open={open === "close"} onOpenChange={opened("close")} />
          ) : (
            <ReopenProblemDialog project={project} problem={problem} open={open === "reopen"} onOpenChange={opened("reopen")} />
          )}
        </>
      ) : null}
    </div>
  );
}

/**
 * One problem (/p/{project}/problems/{code}), for any member with a grant on the project: what hurts and who it hurts,
 * when, how they cope today, how much it hurts (its metric over its window, from its key's signals), the goals that
 * answer it, the claims of the canvases and the plans that point to it, and the Loop's chains that go from it; beside
 * them its owner, rank and revisions. A writer edits it whole, closes it as solved or dropped with a reason, and
 * reopens it; a reader reads.
 */
export function ProblemPage({ project, code, initialError }: { project: string; code: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("problems.page");
  const state = useHubQuery(problemQuery(browserApi, project, code), initialError);
  if (state.status === "error" && state.error.status === 404) {
    return <NotFoundState title={t("notFoundTitle", { code })} description={t("notFoundText")} />;
  }
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(problem) => <ProblemView project={project} problem={problem} />}
    </QueryView>
  );
}
