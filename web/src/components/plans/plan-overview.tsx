"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useEffect, useRef } from "react";

import { NoticeArea, useNotice } from "@/components/admin/notice";
import { Identifier } from "@/components/data/identifier";
import { useCanDispatch, usePlanRunNotice } from "@/components/runs/hooks";
import { PlanRunBanner, RunPlanButton, usePlanActivity } from "@/components/runs/plan-run";
import { LIVE_REFRESH_MS } from "@/components/runs/queries";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { PageSkeleton } from "@/components/states/states";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { planKeys, planQuery } from "@/lib/plan-queries";
import { countSteps, type Plan, type PlanRepo, parsePlan, type PlanView } from "@/lib/plans";

import { PlanHeader, ReadOnlyNotice } from "./plan-header";
import { PlanNotFound } from "./plan-not-found";
import { PlanProgress } from "./progress";
import { Prose, ValueView } from "./prose";
import { RepoStatusBadge } from "./status";
import { formatDay, StepBoard } from "./step-board";

/** The sections the page names in Vietnamese and English; any other key of a plan is shown under its own name. */
const SECTION_NAMES = [
  "acceptance",
  "non_goals",
  "references",
  "seams_touched",
  "risks",
  "rollback",
  "assumptions",
  "decisions",
  "debt",
  "tech_debt",
  "open_questions",
] as const;
type SectionName = (typeof SECTION_NAMES)[number];

/** The order of the keys of a section's items, as `evo_agents.hub.mirror` writes the plan's copy. */
const ITEM_ORDER: Partial<Record<SectionName, readonly string[]>> = {
  acceptance: ["criterion", "what", "status", "verify", "evidence"],
  references: ["what", "where", "why"],
  seams_touched: ["name", "owner", "consumers", "verify", "note", "notes"],
  risks: ["risk", "mitigation"],
  decisions: ["date", "by", "decision", "what", "status", "why", "note"],
  tech_debt: ["issue", "what", "severity", "status", "fixed_at", "note"],
  open_questions: ["issue", "what", "status", "answered_at", "blocking", "note"],
};

function isSectionName(key: string): key is SectionName {
  return (SECTION_NAMES as readonly string[]).includes(key);
}

function Disclosure({ summary, count, children, testId }: { summary: ReactNode; count?: number; children: ReactNode; testId?: string }) {
  return (
    <details className="group rounded-md border bg-card shadow-raised" data-testid={testId}>
      <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 rounded-md px-4 py-2.5 text-sm font-medium hover:bg-muted/50 [&::-webkit-details-marker]:hidden">
        <ChevronRight
          className="size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90"
          aria-hidden="true"
        />
        <span className="min-w-0 flex-1">{summary}</span>
        {count !== undefined ? (
          <span className="font-mono text-xs text-muted-foreground tabular-nums">{count}</span>
        ) : null}
      </summary>
      <div className="border-t px-4 py-3">{children}</div>
    </details>
  );
}

function Intro({ view }: { view: PlanView }) {
  const t = useTranslations("plans.overview");
  const format = useFormatter();
  if (!view.goal && !view.context && !view.createdAt && !view.status) return null;
  return (
    <section aria-labelledby="plan-goal-title" className="flex flex-col gap-3 rounded-md border bg-card shadow-raised p-4">
      <h2 id="plan-goal-title" className="text-[15px] leading-[22px] font-semibold">
        {t("goal")}
      </h2>
      {view.goal ? <Prose>{view.goal}</Prose> : <p className="text-sm text-muted-foreground">{t("noGoal")}</p>}
      {view.status || view.createdAt ? (
        <dl className="flex flex-wrap gap-x-6 gap-y-1 text-xs">
          {view.status ? (
            <div className="flex gap-1.5">
              <dt className="text-muted-foreground">{t("declaredStatus")}</dt>
              <dd className="font-mono">{view.status}</dd>
            </div>
          ) : null}
          {view.createdAt ? (
            <div className="flex gap-1.5">
              <dt className="text-muted-foreground">{t("createdAt")}</dt>
              <dd className="tabular-nums">{formatDay(format, view.createdAt)}</dd>
            </div>
          ) : null}
        </dl>
      ) : null}
      {view.context ? (
        <details className="group">
          <summary className="inline-flex min-h-9 cursor-pointer list-none items-center gap-1.5 rounded-sm text-sm font-medium text-brand [&::-webkit-details-marker]:hidden">
            <ChevronRight className="size-4 transition-transform group-open:rotate-90" aria-hidden="true" />
            {t("context")}
          </summary>
          <Prose className="mt-2">{view.context}</Prose>
        </details>
      ) : null}
    </section>
  );
}

function Repos({ repos }: { repos: PlanRepo[] }) {
  const t = useTranslations("plans.repos");
  const format = useFormatter();
  if (repos.length === 0) return null;
  return (
    <section aria-labelledby="plan-repos-title" className="flex flex-col gap-3">
      <h2 id="plan-repos-title" className="text-[15px] leading-[22px] font-semibold">
        {t("title")}
      </h2>
      <div className="overflow-hidden rounded-md border bg-card shadow-raised">
        <Table scrollLabel={t("title")}>
          <TableCaption className="sr-only">{t("caption")}</TableCaption>
          <TableHeader className="bg-muted">
            <TableRow className="hover:bg-transparent">
              <TableHead className="h-10 px-3 text-xs text-muted-foreground">{t("order")}</TableHead>
              <TableHead className="h-10 px-3 text-xs text-muted-foreground">{t("repo")}</TableHead>
              <TableHead className="h-10 px-3 text-xs text-muted-foreground">{t("status")}</TableHead>
              <TableHead className="hidden h-10 px-3 text-xs text-muted-foreground md:table-cell">{t("branch")}</TableHead>
              <TableHead className="hidden h-10 px-3 text-xs text-muted-foreground lg:table-cell">{t("dependsOn")}</TableHead>
              <TableHead className="hidden h-10 px-3 text-xs text-muted-foreground xl:table-cell">{t("scope")}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {repos.map((repo, index) => (
              <TableRow key={`${repo.repo}-${index}`}>
                <TableCell className="px-3 py-2.5 text-muted-foreground tabular-nums">{repo.order ?? "-"}</TableCell>
                <TableCell className="px-3 py-2.5">
                  <Identifier value={repo.repo} className="text-foreground" />
                </TableCell>
                <TableCell className="px-3 py-2.5">
                  <div className="flex flex-col items-start gap-1">
                    <RepoStatusBadge status={repo.status} />
                    {repo.mergedAt ? (
                      <span className="text-xs text-muted-foreground tabular-nums">
                        {t("mergedAt", { when: formatDay(format, repo.mergedAt) })}
                      </span>
                    ) : null}
                  </div>
                </TableCell>
                <TableCell className="hidden px-3 py-2.5 md:table-cell">
                  {repo.branch ? <Identifier value={repo.branch} /> : <span className="text-muted-foreground">-</span>}
                </TableCell>
                <TableCell className="hidden px-3 py-2.5 lg:table-cell">
                  {repo.dependsOn.length ? (
                    <span className="flex flex-wrap gap-1">
                      {repo.dependsOn.map((name) => (
                        <Identifier key={name} value={name} />
                      ))}
                    </span>
                  ) : (
                    <span className="text-muted-foreground">-</span>
                  )}
                </TableCell>
                <TableCell className="hidden max-w-md px-3 py-2.5 text-xs whitespace-normal text-pretty xl:table-cell">
                  {repo.scope ?? "-"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </section>
  );
}

/** The API keeps a plan as jsonb, which does not keep key order: the named sections come in the order plans are
 * written (as in plan.schema.json), any other key after them, alphabetically. */
function ordered(sections: [string, unknown][]): [string, unknown][] {
  const rank = (key: string) => (isSectionName(key) ? SECTION_NAMES.indexOf(key) : SECTION_NAMES.length);
  return [...sections].sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b));
}

function OtherSections({ sections: given }: { sections: [string, unknown][] }) {
  const t = useTranslations("plans.sections");
  const sections = ordered(given);
  if (sections.length === 0) return null;
  return (
    <section aria-labelledby="plan-sections-title" className="flex flex-col gap-3">
      <h2 id="plan-sections-title" className="text-[15px] leading-[22px] font-semibold">
        {t("title")}
      </h2>
      <div className="flex flex-col gap-2">
        {sections.map(([key, value]) => (
          <Disclosure
            key={key}
            summary={isSectionName(key) ? t(key) : <span className="font-mono">{key}</span>}
            count={Array.isArray(value) ? value.length : undefined}
            testId={`plan-section-${key}`}
          >
            <ValueView value={value} order={isSectionName(key) ? ITEM_ORDER[key] : undefined} />
          </Disclosure>
        ))}
      </div>
    </section>
  );
}

type Activity = ReturnType<typeof usePlanActivity>;

function Overview({ project, plan, activity }: { project: string; plan: Plan; activity: Activity }) {
  const view = parsePlan(plan.body, plan.plan_id);
  const counts = countSteps(view.steps);
  const { notice, show, clear } = useNotice();
  const dispatched = usePlanRunNotice();
  const canDispatch = useCanDispatch(project);
  return (
    <>
      <PlanHeader
        project={project}
        plan={plan}
        current="steps"
        planRunActive={activity.planRun !== null}
        actions={
          plan.area === "active" && canDispatch ? (
            <RunPlanButton
              project={project}
              planId={plan.plan_id}
              pending={counts.pending}
              runs={activity.active}
              loaded={activity.loaded}
              onDispatched={(run) => show(dispatched(run))}
            />
          ) : null
        }
      />
      <ReadOnlyNotice />
      <NoticeArea notice={notice} onDismiss={clear} />
      {activity.planRun ? <PlanRunBanner project={project} run={activity.planRun} steps={view.steps} /> : null}
      <div className="grid gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <Intro view={view} />
        <PlanProgress counts={counts} />
      </div>
      <StepBoard project={project} planId={plan.plan_id} steps={view.steps} />
      <Repos repos={view.repos} />
      <OtherSections sections={view.sections} />
    </>
  );
}

export function PlanOverview({
  project,
  planId,
  initialError,
  invalid = false,
}: {
  project: string;
  planId: string;
  initialError: ApiErrorInfo | null;
  invalid?: boolean;
}) {
  const queryClient = useQueryClient();
  const activity = usePlanActivity(project, planId, !invalid);
  const runId = activity.planRun?.id ?? null;
  // While a plan run works on the plan, its steps change as the run reports them: read the plan as often as the runs.
  const state = useHubQuery(
    { ...planQuery(browserApi, project, planId), enabled: !invalid, refetchInterval: runId !== null ? LIVE_REFRESH_MS : false },
    initialError,
  );
  // Once the run ends, read the plan once more for what it wrote last.
  const previous = useRef<number | null>(null);
  useEffect(() => {
    if (previous.current !== null && runId === null) void queryClient.invalidateQueries({ queryKey: planKeys.one(project, planId), exact: true });
    previous.current = runId;
  }, [runId, queryClient, project, planId]);
  if (invalid || (state.status === "error" && state.error.status === 404)) {
    return <PlanNotFound project={project} planId={planId} />;
  }
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(plan) => <Overview project={project} plan={plan} activity={activity} />}
    </QueryView>
  );
}
