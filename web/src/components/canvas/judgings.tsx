"use client";

import { useQuery } from "@tanstack/react-query";
import { Scale } from "lucide-react";
import type { Route } from "next";
import { useTranslations } from "next-intl";

import { RunRef } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { browserApi } from "@/lib/api/browser";

import { type CanvasJudging, judgingsQuery } from "./api";
import { SyntheticTag, VerdictTag } from "./badges";
import { SectionCard } from "./chrome";

function runHref(project: string, run: number): Route {
  return `/p/${encodeURIComponent(project)}/runs/${run}` as Route;
}

function JudgingItem({ project, judging }: { project: string; judging: CanvasJudging }) {
  const t = useTranslations("canvas.judgings");
  return (
    <li className="flex min-w-0 flex-col gap-1.5 border-t pt-3 first:border-t-0 first:pt-0" data-testid="judging" data-judging={judging.id}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
        <VerdictTag verdict={judging.verdict} />
        <span>{judging.source === "signal" ? t("feedback") : t("evidence", { id: judging.evidence_id ?? 0, kind: judging.kind })}</span>
        {judging.synthetic ? <SyntheticTag /> : null}
        {judging.run_id ? <RunRef id={judging.run_id} href={runHref(project, judging.run_id)} /> : null}
        <span className="ml-auto text-fg-subtle">
          <When value={judging.judged_at ?? judging.created_at} />
        </span>
      </div>
      <blockquote className="border-l-2 pl-3 text-[13px] text-pretty whitespace-pre-line text-foreground [overflow-wrap:anywhere]">{judging.text}</blockquote>
      {judging.reason ? (
        <p className="text-xs text-muted-foreground [overflow-wrap:anywhere]">
          <span className="font-medium text-foreground">{t("why")}</span> {judging.reason}
        </p>
      ) : null}
    </li>
  );
}

/**
 * What @strategist's judging runs read of a claim (feedback, and evidence that is no number) and its verdict on each,
 * with why: the hub sums the verdicts into evidence of kind feedback. Shown only once a claim has been judged.
 */
export function JudgingsCard({ project, slug, code }: { project: string; slug: string; code: string }) {
  const t = useTranslations("canvas.judgings");
  const { data } = useQuery(judgingsQuery(browserApi, project, slug, code));
  if (!data || data.length === 0) return null;
  return (
    <SectionCard
      title={
        <span className="inline-flex items-center gap-2">
          <Scale className="size-4 text-muted-foreground" aria-hidden="true" />
          {t("heading", { count: data.length })}
        </span>
      }
      headingId="claim-judgings-title"
      testId="claim-judgings"
    >
      <ul className="flex min-w-0 flex-col gap-3">
        {data.map((judging) => (
          <JudgingItem key={judging.id} project={project} judging={judging} />
        ))}
      </ul>
    </SectionCard>
  );
}
