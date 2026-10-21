"use client";

import { CalendarDays, ClipboardList } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ChangeEvent, useId } from "react";

import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { ApiErrorState, EmptyState, StatePanel } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { pluginPageHref, projectPluginHref } from "@/lib/plugin-queries";
import { TASKS_BOARD, TASKS_PLUGIN } from "@/lib/core-plugin-links";

import { type PmDaily, pmDailyQuery, validDate } from "./api";
import { BlockedSection, DoneSection, EscalateSection, LateSection, LoadSection, ReviewSection, SprintSection, StaleSection } from "./sections";

/** A project with no task at all has nothing to report: the way to its first one. */
function isBare(daily: PmDaily): boolean {
  return (
    daily.sprint === null &&
    daily.done_use_cases.length + daily.done_tasks.length + daily.late.length + daily.review.length + daily.stale.length + daily.blocked.length + daily.escalate.length + daily.load.length === 0 &&
    Object.values(daily.hidden).every((count) => count === 0)
  );
}

/** The 409 of the route: tasks not enabled, or a config of pm the hub refuses. Says which, and where to fix it. */
function NotReady({ project, error }: { project: string; error: ApiErrorInfo }) {
  const t = useTranslations("pm.notReady");
  return (
    <StatePanel icon={ClipboardList} tone="danger" title={t("title")} description={t("text")} testId="pm-not-ready">
      <p className="basis-full text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="pm-not-ready-reason">
        {error.message}
      </p>
      <Button asChild variant="secondary">
        <Link href={projectPluginHref(project, "tasks")}>{t("tasks")}</Link>
      </Button>
      <Button asChild variant="secondary">
        <Link href={projectPluginHref(project, "pm")}>{t("config")}</Link>
      </Button>
    </StatePanel>
  );
}

function Report({ project, daily }: { project: string; daily: PmDaily }) {
  const t = useTranslations("pm");
  if (isBare(daily)) {
    return (
      <EmptyState icon={ClipboardList} title={t("bareTitle")} description={t("bareText")} testId="pm-bare">
        <Button asChild variant="secondary">
          <Link href={pluginPageHref(project, TASKS_PLUGIN, TASKS_BOARD)}>{t("openBoard")}</Link>
        </Button>
      </EmptyState>
    );
  }
  return (
    <div className="grid min-w-0 gap-4 lg:grid-cols-2" data-testid="pm-report">
      <DoneSection project={project} daily={daily} />
      <SprintSection daily={daily} />
      <BlockedSection project={project} daily={daily} />
      <EscalateSection project={project} daily={daily} />
      <LateSection project={project} daily={daily} />
      <ReviewSection project={project} daily={daily} />
      <StaleSection project={project} daily={daily} />
      <LoadSection project={project} daily={daily} />
    </div>
  );
}

/**
 * The daily report of a project (/p/{project}/x/pm/daily): the eight sections of one day, today in the project's
 * timezone unless `?date=` names another. Each task links to its page in the tasks plugin.
 */
export function DailyPage({ project, title, initialError }: { project: string; title: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("pm");
  const router = useRouter();
  const pathname = usePathname();
  const search = useSearchParams();
  const picked = validDate(search.get("date"));
  const inputId = useId();
  const state = useHubQuery(pmDailyQuery(browserApi, project, picked), picked === null ? initialError : null, { live: true });

  const pick = (event: ChangeEvent<HTMLInputElement>) => {
    const next = validDate(event.target.value);
    router.replace((next ? `${pathname}?date=${next}` : pathname) as never);
  };

  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="pm-daily">
      <PageHeader
        title={title}
        sub={state.status === "success" ? t("sub", { date: state.data.date, previous: state.data.previous_working_day }) : undefined}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Label htmlFor={inputId} className="flex items-center gap-1.5 text-[13px]">
              <CalendarDays aria-hidden="true" className="size-4" />
              {t("day")}
            </Label>
            <Input id={inputId} type="date" value={picked ?? (state.status === "success" ? state.data.date : "")} onChange={pick} className="w-40" data-testid="pm-date" />
            {picked ? (
              <Button variant="secondary" onClick={() => router.replace(pathname as never)} data-testid="pm-today">
                {t("today")}
              </Button>
            ) : null}
          </div>
        }
      />
      {state.status === "error" && state.error.status === 409 ? (
        <NotReady project={project} error={state.error} />
      ) : state.status === "error" ? (
        <ApiErrorState error={state.error} onRetry={state.retry} />
      ) : (
        <QueryView state={state}>{(daily) => <Report project={project} daily={daily} />}</QueryView>
      )}
    </div>
  );
}
