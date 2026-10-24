"use client";

import { IterationCw } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { Identifier, NAME_LINK, Tag } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { ChainList } from "./chain-list";
import { GoalStatusBadge } from "./badges";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { browserApi } from "@/lib/api/browser";
import { goalsHref, goalsQuery, LOOP_REFRESH_MS, loopHref, loopItemsQuery } from "@/lib/loop-queries";
import { type OpportunityEvidence, opportunityEvidenceQuery, opportunityRevisionsQuery, type ProjectOpportunityShown } from "@/lib/opportunity-queries";
import { cn } from "@/lib/utils";

import { AnswerChips } from "./answer-chips";
import { bySignal, isRevisionField } from "./opportunity-model";
import { Panel, PanelSkeleton } from "./opportunity-parts";
import { OpportunityStatusBadge } from "./opportunity-status";

const ROW = "flex min-h-11 min-w-0 flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2";

function Empty({ children }: { children: string }) {
  return <p className="px-4 py-4 text-[13px] text-muted-foreground">{children}</p>;
}

/** One line of evidence: what the source said, where it was read, who said it, whether a writer confirmed it. */
function EvidenceLine({ line }: { line: OpportunityEvidence }) {
  const t = useTranslations("opportunities.evidence");
  return (
    <li className="flex min-w-0 flex-col gap-1 px-4 py-2.5" data-testid="opportunity-evidence-line" data-evidence={line.code}>
      <p className="text-sm text-foreground [overflow-wrap:anywhere]">{line.excerpt}</p>
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-fg-subtle">
        <Identifier value={line.code} />
        <span className="font-mono">{line.source_kind}</span>
        <span className="min-w-0 [overflow-wrap:anywhere]">{line.source_uri}</span>
        {line.speaker ? <span>{t("speaker", { speaker: line.speaker })}</span> : null}
        <Tag>{t(`labels.${line.label === "public" ? "public" : "internal"}`)}</Tag>
        <span data-testid="opportunity-evidence-state">
          {line.confirmed_by ? t("confirmed", { by: line.confirmed_by }) : t("unconfirmed")}
        </span>
      </div>
    </li>
  );
}

/** The evidence of the opportunity, a group for each signal it shows, with the lines of the group under it. */
export function EvidenceSection({ project, opportunity }: { project: string; opportunity: ProjectOpportunityShown }) {
  const t = useTranslations("opportunities.evidence");
  const signals = useTranslations("opportunities.signals");
  const state = useHubQuery(opportunityEvidenceQuery(browserApi, project, opportunity.code));
  return (
    <Panel
      id="opportunity-evidence-title"
      title={t("title")}
      testId="opportunity-evidence"
      aside={<span className="tabular-nums">{t("counts", { lines: opportunity.evidence_count, sources: opportunity.source_count })}</span>}
    >
      <QueryView state={state} loading={<PanelSkeleton />}>
        {(lines) =>
          lines.length === 0 ? (
            <Empty>{t("none")}</Empty>
          ) : (
            <div className="flex flex-col divide-y">
              {bySignal(lines).map((group) => (
                <section key={group.signal} aria-label={t("signalLabel", { signal: signals.has(group.signal as never) ? signals(group.signal as never) : group.signal })} data-testid="opportunity-signal" data-signal={group.signal}>
                  <div className="flex min-h-10 items-center gap-2 bg-surface-sunken px-4 py-2">
                    <h3 className="text-[13px] leading-[18px] font-semibold">
                      {signals.has(group.signal as never) ? signals(group.signal as never) : group.signal}
                    </h3>
                    <span className="text-xs text-fg-subtle tabular-nums">{group.lines.length}</span>
                  </div>
                  <ul className="divide-y">
                    {group.lines.map((line) => (
                      <EvidenceLine key={line.code} line={line} />
                    ))}
                  </ul>
                </section>
              ))}
            </div>
          )
        }
      </QueryView>
    </Panel>
  );
}

/** The problems the opportunity answers, and how many of other projects the reader may not see. */
export function AnswersSection({ project, opportunity }: { project: string; opportunity: ProjectOpportunityShown }) {
  const t = useTranslations("opportunities.page");
  const total = opportunity.answers.length + opportunity.hidden_answers;
  return (
    <Panel id="opportunity-answers-title" title={t("answersTitle")} aside={<span className="tabular-nums">{total}</span>} testId="opportunity-answers-panel">
      {total === 0 ? (
        <Empty>{t("noAnswers")}</Empty>
      ) : (
        <div className="px-4 py-3">
          <AnswerChips project={project} answers={opportunity.answers} hidden={opportunity.hidden_answers} />
        </div>
      )}
    </Panel>
  );
}

/** The goals whose newest revision answers the opportunity, each opening its page. */
export function GoalsSection({ project, code }: { project: string; code: string }) {
  const t = useTranslations("opportunities.page");
  const state = useHubQuery(goalsQuery(browserApi, project));
  return (
    <Panel id="opportunity-goals-title" title={t("goalsTitle")} testId="opportunity-goals">
      <QueryView state={state} loading={<PanelSkeleton />}>
        {(all) => {
          const goals = all.filter((goal) => goal.opportunity === code);
          return goals.length === 0 ? (
            <Empty>{t("noGoals")}</Empty>
          ) : (
            <ul className="divide-y" aria-labelledby="opportunity-goals-title">
              {goals.map((goal) => (
                <li key={goal.code} className={ROW} data-testid="opportunity-goal" data-goal={goal.code}>
                  <Identifier value={goal.code} />
                  <Link href={goalsHref(project, goal.code)} className={cn(NAME_LINK, "min-w-0 flex-1 truncate text-sm")} title={goal.statement}>
                    {goal.statement}
                  </Link>
                  <GoalStatusBadge goal={goal} />
                </li>
              ))}
            </ul>
          );
        }}
      </QueryView>
    </Panel>
  );
}

/** The Loop's work items that go from the opportunity, by chain. */
export function LoopSection({ project, code }: { project: string; code: string }) {
  const t = useTranslations("opportunities.page");
  const found = useHubQuery({
    ...loopItemsQuery(browserApi, project, { state: null, kind: null, goal: null, open: false, opportunity: code }),
    refetchInterval: LOOP_REFRESH_MS,
  });
  return (
    <Panel
      id="opportunity-loop-title"
      title={t("loopTitle")}
      testId="opportunity-loop"
      aside={
        <Link href={loopHref(project, "?view=chain")} className="inline-flex min-h-6 items-center gap-1.5 text-brand transition-colors hover:text-brand-hover">
          <IterationCw className="size-4" aria-hidden="true" />
          {t("openLoop")}
        </Link>
      }
    >
      <QueryView state={found} loading={<PanelSkeleton />}>
        {(data) => (data.items.length === 0 ? <Empty>{t("noItems")}</Empty> : <ChainList project={project} items={data.items} />)}
      </QueryView>
    </Panel>
  );
}

/** Every revision of the opportunity, newest first: its number, status, who wrote it and when, what it changed and why. */
export function RevisionsCard({ project, code }: { project: string; code: string }) {
  const t = useTranslations("opportunities.history");
  const fields = useTranslations("opportunities.fields");
  const state = useHubQuery(opportunityRevisionsQuery(browserApi, project, code));
  return (
    <Panel id="opportunity-history-title" title={t("title")} testId="opportunity-history">
      <QueryView state={state} loading={<PanelSkeleton />}>
        {(revisions) => (
          <ol className="divide-y" aria-labelledby="opportunity-history-title">
            {revisions.map((revision) => {
              const changed = revision.changed.filter(isRevisionField).map((field) => fields(field));
              return (
                <li key={revision.revision} className="flex flex-col gap-1 px-4 py-3" data-testid="opportunity-revision-row" data-revision={revision.revision}>
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <span className="text-sm font-medium">{t("revision", { revision: revision.revision })}</span>
                    <OpportunityStatusBadge status={revision.status} />
                    <span className="ml-auto text-xs text-fg-subtle">
                      <When value={revision.created_at} short />
                    </span>
                  </div>
                  <p className="text-[13px] text-muted-foreground [overflow-wrap:anywhere]">
                    {revision.revision === 1 ? t("created", { by: revision.created_by }) : t("changed", { by: revision.created_by, fields: changed.join(", ") || t("nothing") })}
                  </p>
                  {revision.note ? <p className="text-[13px] text-foreground [overflow-wrap:anywhere]">{revision.note}</p> : null}
                </li>
              );
            })}
          </ol>
        )}
      </QueryView>
    </Panel>
  );
}
