"use client";

import { Columns3, FolderGit2, LayoutList, Waypoints } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useMemo, useState } from "react";

import { SearchField } from "@/components/data/search-field";
import { Segmented } from "@/components/data/segmented";
import { TONE_TEXT } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { useIsMobile } from "@/hooks/use-mobile";
import {
  filterSteps,
  type PlanStep,
  STEP_STATUSES,
  type StepGroup,
  stepLabel,
  stepRepos,
} from "@/lib/plans";
import { cn } from "@/lib/utils";

import { DependsOn, formatDay, type StepContext as Context } from "./depends-on";
import { stepHref } from "./links";
import { BlockingBadge, StepStatusBadge, stepLook, useStepStatusText } from "./status";
import { StepGraph } from "./step-graph-lazy";
import StepTable from "./step-table";
import { useStepView } from "./step-view";

/** A column shows this many cards before "show all", so a long column does not bury the others; fewer on a phone,
 * where the columns stack. */
const COLUMN_LIMIT = 8;
const PHONE_COLUMN_LIMIT = 4;

function StepCard({ step, context }: { step: PlanStep; context: Context }) {
  const t = useTranslations("plans.step");
  const format = useFormatter();
  const label = stepLabel(step);
  return (
    <li
      className="group relative flex flex-col gap-2 rounded-md border bg-card p-3 transition-colors hover:border-brand/40 hover:bg-accent/40"
      data-testid="step-card"
      data-step={step.key}
    >
      <div className="flex items-start gap-2">
        <span className="mt-0.5 shrink-0 font-mono text-xs font-medium text-muted-foreground tabular-nums">
          {step.key}
        </span>
        <Link
          href={stepHref(context.project, context.planId, step.key)}
          className="min-w-0 flex-1 text-sm leading-snug font-medium text-pretty [overflow-wrap:anywhere] after:absolute after:inset-0 after:rounded-md after:content-[''] focus-visible:outline-none focus-visible:after:outline-2 focus-visible:after:outline-offset-2 focus-visible:after:outline-ring"
          aria-label={t("open", { key: step.key, title: label })}
        >
          <span className="line-clamp-3">{label || t("untitled")}</span>
        </Link>
      </div>
      {step.group === "other" ? <StepStatusBadge group="other" raw={step.rawStatus} /> : null}
      <div className="flex flex-wrap items-center gap-1.5">
        {step.repo ? (
          <span className="inline-flex h-5 max-w-full items-center gap-1 rounded-xs bg-surface-sunken px-1.5 font-mono text-xs text-muted-foreground">
            <FolderGit2 className="size-3 shrink-0" aria-hidden="true" />
            <span className="truncate">{step.repo}</span>
          </span>
        ) : null}
        {step.group === "done" ? null : <BlockingBadge blocking={step.blocking} />}
        {step.group === "done" && step.doneAt ? (
          <span className="text-xs text-muted-foreground tabular-nums">{formatDay(format, step.doneAt)}</span>
        ) : null}
      </div>
      <DependsOn step={step} context={context} compact />
    </li>
  );
}

function Column({ group, steps, context }: { group: StepGroup; steps: PlanStep[]; context: Context }) {
  const t = useTranslations("plans.board");
  const label = useStepStatusText();
  const [expanded, setExpanded] = useState(false);
  const listId = useId();
  const look = stepLook(group);
  const limit = useIsMobile() ? PHONE_COLUMN_LIMIT : COLUMN_LIMIT;
  const shown = expanded ? steps : steps.slice(0, limit);
  const headingId = `${listId}-heading`;
  return (
    <section
      aria-labelledby={headingId}
      className="flex min-w-0 flex-col gap-2 rounded-md border bg-muted/30 p-2.5"
      data-testid={`board-column-${group}`}
    >
      <h3 id={headingId} className="flex items-center gap-2 px-1 text-sm font-medium">
        <look.icon className={cn("size-4", TONE_TEXT[look.tone])} aria-hidden="true" />
        {group === "other" ? t("otherColumn") : label(group)}
        <span className="ml-auto font-mono text-xs text-muted-foreground tabular-nums" data-testid="column-count">
          {steps.length}
        </span>
      </h3>
      {steps.length === 0 ? (
        <p className="px-1 py-3 text-center text-xs text-muted-foreground">{t("emptyColumn")}</p>
      ) : (
        <ol id={listId} className="flex flex-col gap-2">
          {shown.map((step) => (
            <StepCard key={`${step.index}-${step.key}`} step={step} context={context} />
          ))}
        </ol>
      )}
      {steps.length > limit ? (
        <Button
          variant="ghost"
          className="w-full text-muted-foreground"
          aria-expanded={expanded}
          aria-controls={listId}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? t("showFewer") : t("showAll", { count: steps.length })}
        </Button>
      ) : null}
    </section>
  );
}

/**
 * The plan's steps by status (pending, in progress, blocked, done), as a list in plan order, or as the graph of their
 * dependencies; the choice is in the URL's `view` and remembered (step-view.ts).
 */
export function StepBoard({ project, planId, steps }: { project: string; planId: string; steps: PlanStep[] }) {
  const t = useTranslations("plans.board");
  const [query, setQuery] = useState("");
  const [repo, setRepo] = useState("");
  const [view, setView] = useStepView();
  const repoId = useId();
  const context = useMemo<Context>(
    () => ({ project, planId, byKey: new Map(steps.map((step) => [step.key, step])) }),
    [project, planId, steps],
  );
  const repos = stepRepos(steps);
  const shown = filterSteps(steps, query, repo);
  const groups: StepGroup[] = [...STEP_STATUSES];
  if (steps.some((step) => step.group === "other")) groups.push("other");
  const filtering = query.trim() !== "" || repo !== "";

  return (
    <section aria-labelledby="plan-steps-title" className="flex flex-col gap-3">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <h2 id="plan-steps-title" className="text-[15px] leading-[22px] font-semibold">
          {t("title")}
        </h2>
        <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
          <SearchField
            value={query}
            onCommit={setQuery}
            label={t("search")}
            placeholder={t("searchPlaceholder")}
            clearLabel={t("clearSearch")}
            debounce={150}
            className="sm:w-60"
            testId="steps-search"
          />
          {repos.length > 1 ? (
            <div className="flex items-center gap-2">
              <label htmlFor={repoId} className="text-[13px] text-muted-foreground">
                {t("repo")}
              </label>
              <NativeSelect
                id={repoId}
                value={repo}
                onChange={(event) => setRepo(event.target.value)}
                className="min-w-0 flex-1 sm:flex-none [&>select]:max-md:h-11"
                data-testid="steps-repo"
              >
                <NativeSelectOption value="">{t("allRepos")}</NativeSelectOption>
                {repos.map((name) => (
                  <NativeSelectOption key={name} value={name}>
                    {name}
                  </NativeSelectOption>
                ))}
              </NativeSelect>
            </div>
          ) : null}
          <Segmented
            label={t("view")}
            value={view}
            onChange={setView}
            options={[
              { value: "board", label: t("viewBoard"), icon: Columns3, testId: "steps-view-board" },
              { value: "list", label: t("viewList"), icon: LayoutList, testId: "steps-view-list" },
              { value: "graph", label: t("viewGraph"), icon: Waypoints, testId: "steps-view-graph" },
            ]}
          />
        </div>
      </div>
      <p className="sr-only" aria-live="polite">
        {filtering ? t("shown", { shown: shown.length, total: steps.length }) : ""}
      </p>
      {view === "board" ? (
        <div
          className={cn("grid gap-3 md:grid-cols-2", groups.length > 4 ? "2xl:grid-cols-5 xl:grid-cols-3" : "xl:grid-cols-4")}
          data-testid="step-board"
        >
          {groups.map((group) => (
            <Column
              key={group}
              group={group}
              steps={shown.filter((step) => step.group === group)}
              context={context}
            />
          ))}
        </div>
      ) : view === "list" ? (
        <StepTable steps={shown} context={context} />
      ) : (
        <StepGraph
          steps={steps}
          matching={filtering ? new Set(shown.map((step) => step.key)) : null}
          onBoard={() => setView("board")}
        />
      )}
    </section>
  );
}
