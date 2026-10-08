"use client";

import { useQuery } from "@tanstack/react-query";
import { History, Info, Lock, PencilLine } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { When } from "@/components/admin/when";
import { Identifier } from "@/components/data/identifier";
import { CARD_LINK, HomeCard } from "@/components/home/parts";
import { planHref } from "@/components/plans/links";
import { Prose } from "@/components/plans/prose";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { PageSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { whoamiQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { CharterFormView } from "./charter-form";
import { CuratorHeader, CuratorTabs } from "./curator-header";
import { useCuratorStatus } from "./curator-page";
import { useCuratorRights } from "./hooks";
import { readCharterView } from "./model";
import { Fact, Facts, useMoney } from "./parts";
import { type Charter, charterQuery, charterRevisionsQuery, type CharterRevision, curatorHref, type CuratorStatus } from "./queries";

function charterHref(project: string, revision: number | null = null, edit = false): Route {
  const base = curatorHref(project, "charter");
  if (edit) return `${base}?edit=1` as Route;
  return (revision === null ? base : `${base}?revision=${revision}`) as Route;
}

function List({ items, mono = false, empty, testId }: { items: readonly ReactNode[]; mono?: boolean; empty: string; testId?: string }) {
  if (items.length === 0) return <span className="text-muted-foreground">{empty}</span>;
  return (
    <ul className={cn("flex flex-col gap-1", mono && "font-mono text-xs")} data-testid={testId}>
      {items.map((item, index) => (
        <li key={index} className="[overflow-wrap:anywhere]">
          {item}
        </li>
      ))}
    </ul>
  );
}

/** The charter as it reads: every setting the night shift follows, grouped as the form groups them. */
function CharterView({ project, charter }: { project: string; charter: Charter }) {
  const t = useTranslations("curator.charter");
  const tRuntime = useTranslations("runs.runtime");
  const money = useMoney();
  const role = (value: { runtime: string; model?: string | null } | undefined) =>
    t("roleValue", { runtime: tRuntime((value?.runtime ?? "claude-code") as "claude-code"), model: value?.model ?? t("modelDefault") });
  const hidden = charter.judge?.hidden_checks;
  return (
    <div className="grid items-start gap-4 lg:grid-cols-2" data-testid="charter-view" data-revision={charter.revision}>
      <HomeCard title={t("groups.goals")} testId="charter-view-goals">
        {(charter.goals ?? []).length === 0 ? (
          <p className="px-4 py-3 text-[13px] text-muted-foreground">{t("noGoals")}</p>
        ) : (
          <ol className="flex flex-col divide-y">
            {(charter.goals ?? []).map((goal, index) => (
              <li key={goal.id} className="flex min-w-0 flex-col gap-1 px-4 py-3">
                <span className="flex items-center gap-2 text-xs text-muted-foreground">
                  <span className="tabular-nums">{index + 1}.</span>
                  <Identifier value={goal.id} />
                </span>
                <Prose className="text-[13px]">{goal.what}</Prose>
              </li>
            ))}
          </ol>
        )}
      </HomeCard>
      <HomeCard title={t("groups.window")} testId="charter-view-window">
        <Facts>
          <Fact label={t("fields.window")} testId="charter-window">
            {t("windowValue", { start: charter.window.start, end: charter.window.end, timezone: charter.window.timezone })}
          </Fact>
          <Fact label={t("fields.worker")}>
            <Identifier value={charter.worker} testId="charter-worker-name" />
          </Fact>
          <Fact label={t("fields.owner")}>
            <span className="font-mono text-xs">{charter.schedule_owner}</span>
          </Fact>
          <Fact label={t("fields.briefAt")}>{charter.brief_at ?? "07:00"}</Fact>
        </Facts>
      </HomeCard>
      <HomeCard title={t("groups.budget")} testId="charter-view-budget">
        <Facts>
          <Fact label={t("fields.nightBudget")} testId="charter-night-budget-value">
            {money(charter.night_budget_usd)}
          </Fact>
          <Fact label={t("fields.runBudget")}>{charter.run_budget_usd ? money(charter.run_budget_usd) : t("runBudgetNone")}</Fact>
          <Fact label={t("fields.maxRunsPerNight")}>{charter.max_runs_per_night ?? 6}</Fact>
          <Fact label={t("fields.runMaxTurns")}>{charter.run_max_turns ?? 300}</Fact>
          <Fact label={t("fields.runMinutes")}>{t("minutes", { minutes: charter.run_minutes ?? 120 })}</Fact>
          <Fact label={t("fields.maxFailedInARow")}>{t("circuitValue", { count: charter.circuit_breaker?.max_failed_in_a_row ?? 2 })}</Fact>
        </Facts>
      </HomeCard>
      <HomeCard title={t("groups.scope")} testId="charter-view-scope">
        <Facts>
          <Fact label={t("fields.nightPlans")}>
            <List
              items={(charter.night_plans ?? []).map((plan) => (
                <Link key={plan} href={planHref(project, plan)} className="font-mono text-xs text-brand underline-offset-4 hover:underline">
                  {plan}
                </Link>
              ))}
              empty={t("nightPlansNone")}
              testId="charter-night-plans-value"
            />
          </Fact>
          <Fact label={t("fields.maxDecisionsPerDay")}>{charter.max_decisions_per_day ?? 5}</Fact>
          <Fact label={t("fields.autoMergeShort")} testId="charter-auto-merge-value">
            {(charter.auto_merge ?? []).length > 0 ? t("autoMergeOn", { tiers: (charter.auto_merge ?? []).join(", ") }) : t("autoMergeOff")}
          </Fact>
          <Fact label={t("fields.outcomeDays")} testId="charter-outcome-days-value">
            {t("outcomeDaysValue", { count: charter.outcome_days ?? 7 })}
          </Fact>
          <Fact label={t("fields.protectedPaths")}>
            <List items={charter.protected_paths ?? []} mono empty={t("protectedNone")} testId="charter-protected-value" />
          </Fact>
        </Facts>
      </HomeCard>
      <HomeCard title={t("groups.review")} testId="charter-view-review">
        <Facts>
          <Fact label={t("fields.reviewLenses")}>{charter.review?.lenses ?? 3}</Fact>
          <Fact label={t("fields.reviewDays")}>{t("days", { days: charter.review?.days ?? 7 })}</Fact>
          <Fact label={t("fields.reviewBudget")}>{charter.review?.budget_usd ? money(charter.review.budget_usd) : t("reviewBudgetNone")}</Fact>
        </Facts>
      </HomeCard>
      <HomeCard title={t("groups.roles")} testId="charter-view-roles">
        <Facts>
          <Fact label={t("roles.reviewer")}>{role(charter.reviewer)}</Fact>
          <Fact label={t("roles.builder")}>{role(charter.builder)}</Fact>
          <Fact label={t("roles.judge")}>{role(charter.judge)}</Fact>
          <Fact label={t("fields.hiddenChecks")} testId="charter-hidden-checks-value">
            {hidden === null || hidden === undefined ? (
              <span className="inline-flex items-center gap-1.5 text-muted-foreground">
                <Lock className="size-3.5" aria-hidden="true" />
                {t("hiddenForAdmins")}
              </span>
            ) : (
              <List items={hidden} mono empty={t("hiddenNone")} />
            )}
          </Fact>
        </Facts>
      </HomeCard>
    </div>
  );
}

/** Every revision of the charter, newest first; none is ever deleted. The one shown is marked. */
function Revisions({ project, shown }: { project: string; shown: number }) {
  const t = useTranslations("curator.charter");
  const revisions = useQuery(charterRevisionsQuery(browserApi, project));
  const list: CharterRevision[] = revisions.data ?? [];
  return (
    <HomeCard title={t("revisions.title")} count={{ value: list.length, tone: "neutral", words: t("revisions.count", { count: list.length }) }} testId="charter-revisions">
      {revisions.isPending ? (
        <p className="px-4 py-3 text-[13px] text-muted-foreground">{t("revisions.loading")}</p>
      ) : (
        <ol className="flex flex-col">
          {list.map((revision) => {
            const current = revision.revision === shown;
            return (
              <li key={revision.revision} className={cn("border-t first:border-t-0", current && "bg-surface-selected")}>
                <Link
                  href={charterHref(project, revision.revision === list[0]?.revision ? null : revision.revision)}
                  aria-current={current ? "page" : undefined}
                  className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-[13px] transition-colors hover:bg-accent max-md:min-h-11"
                  data-testid="charter-revision"
                  data-revision={revision.revision}
                >
                  <span className="font-medium tabular-nums">{t("revisions.number", { revision: revision.revision })}</span>
                  <span className="font-mono text-xs text-muted-foreground">{revision.updated_by}</span>
                  <span className="text-xs text-fg-subtle">
                    <When value={revision.updated_at} short />
                  </span>
                  <span className="ml-auto text-xs text-fg-subtle">{t("revisions.worker", { worker: revision.worker })}</span>
                </Link>
              </li>
            );
          })}
        </ol>
      )}
    </HomeCard>
  );
}

function CharterBody({ project, status, revision, edit }: { project: string; status: CuratorStatus; revision: number | null; edit: boolean }) {
  const t = useTranslations("curator.charter");
  const rights = useCuratorRights(project, status);
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const charter = useHubQuery(charterQuery(browserApi, project, revision));
  if (edit && rights.charter) {
    return (
      <QueryView state={charter} loading={<PageSkeleton />}>
        {(found) => <CharterFormView project={project} charter={found} login={me?.login ?? null} />}
      </QueryView>
    );
  }
  return (
    <QueryView state={charter} loading={<PageSkeleton />}>
      {(found) =>
        found === null ? (
          <div role="note" className="flex flex-wrap items-center gap-3 rounded-md border bg-surface-sunken px-4 py-3 text-[13px] text-muted-foreground" data-testid="charter-none">
            <Info className="size-4 shrink-0" aria-hidden="true" />
            <p className="min-w-0 flex-1 text-pretty">{rights.charter ? t("noneAdmin") : t("noneReader")}</p>
            {rights.charter ? (
              <Button asChild size="sm">
                <Link href={charterHref(project, null, true)} data-testid="charter-create">
                  <PencilLine aria-hidden="true" />
                  {t("create")}
                </Link>
              </Button>
            ) : null}
          </div>
        ) : (
          <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_20rem]">
            <div className="flex min-w-0 flex-col gap-4">
              {revision !== null ? (
                <div role="note" className="flex flex-wrap items-center gap-3 rounded-md border bg-surface-sunken px-4 py-3 text-[13px]" data-testid="charter-old-revision">
                  <History className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                  <p className="min-w-0 flex-1 text-pretty text-muted-foreground">
                    {t("oldRevision", { revision: found.revision, login: found.updated_by })}
                  </p>
                  <Link href={charterHref(project)} className={CARD_LINK}>
                    {t("showNewest")}
                  </Link>
                </div>
              ) : !rights.charter ? (
                <div role="note" className="flex items-start gap-2.5 rounded-md border bg-surface-sunken px-4 py-3 text-[13px] text-muted-foreground" data-testid="charter-read-only">
                  <Lock className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                  <p className="text-pretty">{t("readOnly")}</p>
                </div>
              ) : null}
              <CharterView project={project} charter={found} />
            </div>
            <Revisions project={project} shown={found.revision} />
          </div>
        )
      }
    </QueryView>
  );
}

/**
 * A project's charter: every setting of its night shift, read by any member, written by an admin of the project as a
 * new revision through the form (`?edit=1`); every revision is kept and readable (`?revision=N`).
 */
export function CharterPage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("curator.charter");
  const params = useSearchParams();
  const { revision, edit } = readCharterView(params);
  const status = useCuratorStatus(project, initialError);
  const rights = useCuratorRights(project, status.status === "success" ? status.data : null);
  return (
    <QueryView state={status} loading={<PageSkeleton />}>
      {(found) => (
        <div className="flex flex-col gap-6" data-testid="charter-page" data-mode={edit && rights.charter ? "edit" : "view"}>
          <CuratorHeader
            project={project}
            status={found}
            actions={
              rights.charter && !edit && found.charter !== null ? (
                <Button asChild variant="secondary">
                  <Link href={charterHref(project, null, true)} data-testid="charter-edit">
                    <PencilLine aria-hidden="true" />
                    {t("edit")}
                  </Link>
                </Button>
              ) : null
            }
          />
          <CuratorTabs project={project} waiting={found.open_proposals} />
          <CharterBody project={project} status={found} revision={revision} edit={edit} />
        </div>
      )}
    </QueryView>
  );
}
