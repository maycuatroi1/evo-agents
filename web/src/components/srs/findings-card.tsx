"use client";

import { Bot, RotateCw, SearchCheck, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useId } from "react";

import { RunRef } from "@/components/data/identifier";
import { Banner } from "@/components/feedback/banner";
import { projectHref } from "@/components/shell/nav";
import { ListSkeleton, LoadingState } from "@/components/states/states";
import { StatusBadge } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

import type { SrsFinding, SrsReviewRun } from "./api";
import { FindingItem } from "./finding-item";
import { groupFindings, reviewBroke, reviewHolds } from "./findings-model";

/** @ba's review run of a round: `#431`, linking to the run's page, and its state as a pill. */
export function ReviewRunRef({ project, run }: { project: string; run: SrsReviewRun }) {
  const t = useTranslations("srs.findings");
  return (
    <span className="inline-flex flex-wrap items-center gap-2" data-testid="srs-review-run" data-run-id={run.id} data-state={run.state}>
      <RunRef id={run.id} href={projectHref(project, `runs/${run.id}`)} label={t("openRun", { id: run.id })} />
      <StatusBadge kind="run" status={run.state} />
    </span>
  );
}

/** A group of findings under its heading and count: the ones to decide, the other open ones, the settled ones. */
function Group({ title, count, tone, testId, children }: { title: string; count: number; tone: "attention" | "neutral"; testId: string; children: ReactNode }) {
  const id = useId();
  return (
    <section aria-labelledby={id} className="flex min-w-0 flex-col gap-2" data-testid={testId} data-count={count}>
      <h3 id={id} className="flex items-center gap-2 text-[13px] leading-[18px] font-medium text-foreground">
        {title}
        <span
          className={cn(
            "inline-flex h-5 min-w-5 items-center justify-center rounded-full px-1.5 text-xs font-medium tabular-nums",
            tone === "attention" ? "bg-attention-soft text-attention" : "bg-muted text-muted-foreground",
          )}
          data-testid={`${testId}-count`}
        >
          {count}
        </span>
      </h3>
      <ul className="flex min-w-0 flex-col divide-y rounded-md border bg-card" role="list">
        {children}
      </ul>
    </section>
  );
}

export type FindingsRead = { data: SrsFinding[] | undefined; isPending: boolean; isError: boolean; refetch: () => unknown };

/**
 * The Findings card of a version's review page (criterion a14 of plan srs-conflicts-and-tasks): the conflicts @ba found
 * in the document, the open ones to decide first with their count, then the other open ones, then the settled ones with
 * who settled them, when and the message that did. While @ba's review of the round is queued or running the card says
 * so and the page reads the findings again until it ends; a review of a version in review that failed, was lost or
 * cancelled is said too.
 * Readers see it as writers do; nothing on it settles or waives a finding (that is the settle chat's and an admin's
 * `evo-agents hub srs waive`).
 */
export function FindingsCard({
  project,
  slug,
  round,
  run,
  inReview,
  findings,
}: {
  project: string;
  slug: string;
  round: number;
  run: SrsReviewRun | null;
  inReview: boolean;
  findings: FindingsRead;
}) {
  const t = useTranslations("srs.findings");
  const groups = findings.data ? groupFindings(findings.data) : null;
  const total = findings.data?.length ?? 0;
  return (
    <Card size="sm" data-testid="srs-findings" data-state={findings.data ? "ready" : findings.isError ? "error" : "loading"}>
      <CardHeader>
        <CardTitle>
          <h2>{t("title")}</h2>
        </CardTitle>
        <p className="text-xs text-fg-subtle">{t("description")}</p>
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-4">
        {reviewHolds(run) ? (
          <Banner icon={Bot} role="status" title={t("reviewing", { round })} actions={<ReviewRunRef project={project} run={run} />} testId="srs-findings-reviewing">
            {t("reviewingText")}
          </Banner>
        ) : inReview && reviewBroke(run) ? (
          <Banner icon={TriangleAlert} title={t("reviewBroke", { round })} actions={<ReviewRunRef project={project} run={run} />} testId="srs-findings-review-broke">
            {t("reviewBrokeText")}
          </Banner>
        ) : null}
        {groups ? (
          total === 0 ? (
            <div className="flex items-start gap-2.5 text-[13px] leading-[18px]" data-testid="srs-findings-empty">
              <SearchCheck className="mt-px size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
              <div className="flex min-w-0 flex-col gap-0.5">
                <p className="font-medium text-foreground">{t("emptyTitle")}</p>
                <p className="text-muted-foreground">{reviewHolds(run) ? t("emptyReviewing") : t("emptyText")}</p>
              </div>
            </div>
          ) : (
            <>
              <p className="text-xs text-muted-foreground" aria-live="polite" data-testid="srs-findings-counts">
                {t("counts", { decide: groups.decide.length, open: groups.open.length, settled: groups.settled.length })}
              </p>
              {groups.decide.length > 0 ? (
                <Group title={t("groups.decide")} count={groups.decide.length} tone="attention" testId="srs-findings-decide">
                  {groups.decide.map((finding) => (
                    <FindingItem key={finding.id} project={project} slug={slug} finding={finding} />
                  ))}
                </Group>
              ) : null}
              {groups.open.length > 0 ? (
                <Group title={t("groups.open")} count={groups.open.length} tone="neutral" testId="srs-findings-open">
                  {groups.open.map((finding) => (
                    <FindingItem key={finding.id} project={project} slug={slug} finding={finding} />
                  ))}
                </Group>
              ) : null}
              {groups.settled.length > 0 ? (
                <Group title={t("groups.settled")} count={groups.settled.length} tone="neutral" testId="srs-findings-settled">
                  {groups.settled.map((finding) => (
                    <FindingItem key={finding.id} project={project} slug={slug} finding={finding} />
                  ))}
                </Group>
              ) : null}
            </>
          )
        ) : findings.isError ? (
          <div role="alert" className="flex flex-wrap items-center gap-x-3 gap-y-2 text-[13px] leading-[18px] text-danger" data-testid="srs-findings-error">
            <TriangleAlert className="size-4 shrink-0" aria-hidden="true" />
            <span className="min-w-0 flex-1 basis-48">{t("failed")}</span>
            <Button type="button" variant="secondary" size="sm" onClick={() => void findings.refetch()}>
              <RotateCw aria-hidden="true" />
              {t("retry")}
            </Button>
          </div>
        ) : (
          <LoadingState>
            <ListSkeleton rows={2} />
          </LoadingState>
        )}
      </CardContent>
    </Card>
  );
}
