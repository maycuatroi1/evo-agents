"use client";

import { Bot, ChevronRight, RotateCw, SearchCheck, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Banner } from "@/components/feedback/banner";
import { ListSkeleton, LoadingState } from "@/components/states/states";
import { Button } from "@/components/ui/button";

import type { SrsFinding, SrsReviewRun } from "./api";
import type { CaseFindings } from "./case-review-model";
import { FindingChoice, FoldedSources } from "./finding-choice";
import type { FindingsRead } from "./findings-card";
import { reviewBroke, reviewHolds } from "./findings-model";
import { StepCard } from "./step-card";

/** A folded group of findings under a line that counts them: those to decide elsewhere, or the advice. */
function Fold({ title, text, tone, testId, children }: { title: string; text: string; tone: "attention" | "neutral"; testId: string; children: ReactNode }) {
  return (
    <details className="group rounded-md border" data-testid={testId}>
      <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 px-3 text-sm font-medium [&::-webkit-details-marker]:hidden">
        <ChevronRight className="size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" aria-hidden="true" />
        <span className={tone === "attention" ? "text-attention" : undefined}>{title}</span>
      </summary>
      <div className="flex flex-col gap-3 border-t p-3">
        <p className="text-xs text-muted-foreground">{text}</p>
        {children}
      </div>
    </details>
  );
}

/** A finding given as advice: its summary and its sources, folded. */
function Advice({ project, slug, finding }: { project: string; slug: string; finding: SrsFinding }) {
  return (
    <li className="flex min-w-0 flex-col gap-1 py-3 text-[13px] first:pt-0 last:pb-0" data-testid="srs-case-advice" data-finding={finding.id}>
      <p className="[overflow-wrap:anywhere]">{finding.summary}</p>
      <FoldedSources project={project} slug={slug} finding={finding} />
    </li>
  );
}

/**
 * Step 3, what @ba noted: the points to decide about this use case or a business rule under it, each a question with
 * its choices; then, folded, the points to decide elsewhere in the document (approval waits for those too) and the
 * other notes, advice only. While @ba still checks the version the card says so and the page reads the findings again;
 * with nothing to settle it says that in one line.
 */
export function CaseFindingsCard({
  project,
  slug,
  caseKey,
  run,
  findings,
  groups,
  writer,
}: {
  project: string;
  slug: string;
  caseKey: string;
  run: SrsReviewRun | null;
  findings: FindingsRead;
  groups: CaseFindings | null;
  writer: boolean;
}) {
  const t = useTranslations("srs.caseReview");
  const reviewing = reviewHolds(run);
  const empty = groups !== null && groups.decide.length === 0 && groups.elsewhere.length === 0 && groups.advice.length === 0;
  return (
    <StepCard id="srs-step-ba" title={t("step.ba")} note={groups && groups.decide.length > 0 ? t("ba.intro") : undefined} testId="srs-case-ba">
      {reviewing ? (
        <Banner icon={Bot} role="status" title={t("ba.reviewing")} testId="srs-case-ba-reviewing">
          {t("ba.reviewingText")}
        </Banner>
      ) : reviewBroke(run) ? (
        <Banner icon={TriangleAlert} title={t("ba.broke")} testId="srs-case-ba-broke" />
      ) : null}
      {groups ? (
        <>
          {empty && !reviewing ? (
            <p className="flex items-center gap-2 text-[13px] text-muted-foreground" data-testid="srs-case-ba-none">
              <SearchCheck className="size-4 shrink-0" aria-hidden="true" />
              {run ? t("ba.none") : t("ba.noneWithout")}
            </p>
          ) : null}
          {groups.decide.length > 0 ? (
            <ul className="flex flex-col divide-y" role="list" aria-label={t("ba.decideLabel")} data-testid="srs-case-decide" data-count={groups.decide.length}>
              {groups.decide.map((finding) => (
                <FindingChoice key={finding.id} project={project} slug={slug} caseKey={caseKey} finding={finding} writer={writer} />
              ))}
            </ul>
          ) : null}
          {groups.elsewhere.length > 0 ? (
            <Fold title={t("ba.elsewhere", { count: groups.elsewhere.length })} text={t("ba.elsewhereText")} tone="attention" testId="srs-case-elsewhere">
              <ul className="flex flex-col divide-y" role="list">
                {groups.elsewhere.map((finding) => (
                  <FindingChoice key={finding.id} project={project} slug={slug} caseKey={caseKey} finding={finding} writer={writer} />
                ))}
              </ul>
            </Fold>
          ) : null}
          {groups.advice.length > 0 ? (
            <Fold title={t("ba.advice", { count: groups.advice.length })} text={t("ba.adviceText")} tone="neutral" testId="srs-case-advice-list">
              <ul className="flex flex-col divide-y" role="list">
                {groups.advice.map((finding) => (
                  <Advice key={finding.id} project={project} slug={slug} finding={finding} />
                ))}
              </ul>
            </Fold>
          ) : null}
        </>
      ) : findings.isError ? (
        <div role="alert" className="flex flex-wrap items-center gap-x-3 gap-y-2 text-[13px] text-danger" data-testid="srs-case-ba-error">
          <TriangleAlert className="size-4 shrink-0" aria-hidden="true" />
          <span className="min-w-0 flex-1 basis-48">{t("ba.failed")}</span>
          <Button type="button" variant="secondary" size="sm" onClick={() => void findings.refetch()}>
            <RotateCw aria-hidden="true" />
            {t("ba.retry")}
          </Button>
        </div>
      ) : (
        <LoadingState>
          <ListSkeleton rows={2} />
        </LoadingState>
      )}
    </StepCard>
  );
}
