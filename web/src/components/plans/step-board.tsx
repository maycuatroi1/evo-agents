"use client";

import { Columns3, CornerDownRight, FolderGit2, LayoutList, Search } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useMemo, useState } from "react";

import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";
import { TONE_TEXT } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useIsMobile } from "@/hooks/use-mobile";
import {
  filterSteps,
  type PlanStep,
  STEP_STATUSES,
  STEP_GROUPS,
  type StepGroup,
  stepLabel,
  stepRepos,
} from "@/lib/plans";
import { cn } from "@/lib/utils";

import { stepHref } from "./links";
import { BlockingBadge, StepStatusBadge, StepStatusIcon, stepLook, useStepStatusText } from "./status";

/** A column shows this many cards before "show all", so a long column does not bury the others; fewer on a phone,
 * where the columns stack. */
const COLUMN_LIMIT = 8;
const PHONE_COLUMN_LIMIT = 4;

type Context = { project: string; planId: string; byKey: Map<string, PlanStep> };

/** depends_on as links to those steps, each with its status, so a reader sees what a step waits for. */
export function DependsOn({ step, context, compact = false }: { step: PlanStep; context: Context; compact?: boolean }) {
  const t = useTranslations("plans.step");
  if (step.dependsOn.length === 0) {
    return compact ? null : <span className="text-sm text-muted-foreground">{t("noDependencies")}</span>;
  }
  return (
    <span className={cn("flex flex-wrap items-center gap-x-2 gap-y-1", compact ? "text-xs" : "text-sm")}>
      {compact ? (
        <span className="inline-flex items-center gap-1 text-muted-foreground">
          <CornerDownRight className="size-3.5" aria-hidden="true" />
          {t("after")}
        </span>
      ) : null}
      {step.dependsOn.map((key) => {
        const target = context.byKey.get(key);
        if (!target) {
          return (
            <span key={key} className="font-mono text-muted-foreground" title={t("unknownStep")}>
              {key}
            </span>
          );
        }
        return (
          <Link
            key={key}
            href={stepHref(context.project, context.planId, key)}
            className="relative z-10 inline-flex items-center gap-1 rounded font-mono text-brand underline-offset-4 hover:underline"
            aria-label={t("dependencyLink", { key, title: stepLabel(target) })}
          >
            <StepStatusIcon group={target.group} raw={target.rawStatus} className="[&_svg]:size-3.5" />
            {key}
          </Link>
        );
      })}
    </span>
  );
}

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

type Formatter = ReturnType<typeof useFormatter>;

/** A plan date (often `2026-10-02T11:15:00+07:00`, sometimes a bare day or free text) for people to read. */
export function formatDay(format: Formatter, value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return /T\d/.test(value)
    ? format.dateTime(date, { dateStyle: "medium", timeStyle: "short" })
    : format.dateTime(date, { dateStyle: "medium" });
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

function StepTable({ steps, context }: { steps: PlanStep[]; context: Context }) {
  const t = useTranslations("plans.board");
  const format = useFormatter();
  const columns = useMemo(() => {
    const helper = dataTableColumns<PlanStep>();
    return helper.columns([
      helper.accessor("index", {
        header: () => t("columns.step"),
        sortFn: "basic",
        cell: (info) => {
          const step = info.row.original;
          return (
            <div className="flex min-w-48 items-start gap-2">
              <span className="mt-0.5 font-mono text-xs text-muted-foreground tabular-nums">{step.key}</span>
              <Link
                href={stepHref(context.project, context.planId, step.key)}
                className={cn(NAME_LINK, "text-pretty [overflow-wrap:anywhere]")}
              >
                {stepLabel(step) || step.key}
              </Link>
            </div>
          );
        },
      }),
      helper.accessor((row) => STEP_GROUPS.indexOf(row.group), {
        id: "status",
        header: () => t("columns.status"),
        sortFn: "basic",
        cell: (info) => (
          <div className="flex flex-wrap gap-1">
            <StepStatusBadge group={info.row.original.group} raw={info.row.original.rawStatus} />
            <BlockingBadge blocking={info.row.original.blocking} />
          </div>
        ),
      }),
      helper.accessor((row) => row.repo ?? "", {
        id: "repo",
        header: () => t("columns.repo"),
        sortFn: "text",
        cell: (info) => <span className="font-mono text-xs">{info.getValue() || "-"}</span>,
      }),
      helper.accessor((row) => row.dependsOn.join(", "), {
        id: "depends",
        header: () => t("columns.dependsOn"),
        enableSorting: false,
        cell: (info) => <DependsOn step={info.row.original} context={context} />,
      }),
      helper.accessor((row) => row.doneAt ?? "", {
        id: "done_at",
        header: () => t("columns.doneAt"),
        sortFn: "text",
        cell: (info) => (
          <span className="text-xs text-muted-foreground tabular-nums">
            {info.getValue() ? formatDay(format, info.getValue()) : "-"}
          </span>
        ),
      }),
    ]);
  }, [t, format, context]);
  return (
    <DataTable
      data={steps}
      columns={columns}
      caption={t("tableCaption")}
      getRowId={(row) => `${row.index}`}
      initialSorting={[{ id: "index", desc: false }]}
      columnClassNames={{ repo: "hidden md:table-cell", depends: "hidden lg:table-cell", done_at: "hidden xl:table-cell" }}
      testId="steps-table"
      empty={t("noMatch")}
    />
  );
}

type View = "board" | "list";

/** The plan's steps by status (pending, in progress, blocked, done), or as a list in plan order. */
export function StepBoard({ project, planId, steps }: { project: string; planId: string; steps: PlanStep[] }) {
  const t = useTranslations("plans.board");
  const [query, setQuery] = useState("");
  const [repo, setRepo] = useState("");
  const [view, setView] = useState<View>("board");
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
        <h2 id="plan-steps-title" className="text-base font-medium">
          {t("title")}
        </h2>
        <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
          <div className="relative sm:w-60">
            <Search
              className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t("searchPlaceholder")}
              aria-label={t("search")}
              className="h-10 pl-8"
              data-testid="steps-search"
            />
          </div>
          {repos.length > 1 ? (
            <div className="flex items-center gap-2">
              <label htmlFor={repoId} className="text-sm text-muted-foreground">
                {t("repo")}
              </label>
              <select
                id={repoId}
                value={repo}
                onChange={(event) => setRepo(event.target.value)}
                className="h-10 min-w-0 flex-1 rounded-md border border-input bg-background px-2.5 font-mono text-sm sm:flex-none"
                data-testid="steps-repo"
              >
                <option value="">{t("allRepos")}</option>
                {repos.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </div>
          ) : null}
          <div role="group" aria-label={t("view")} className="inline-flex w-fit rounded-md border bg-muted/40 p-0.5">
            {(
              [
                ["board", Columns3],
                ["list", LayoutList],
              ] as const
            ).map(([id, Icon]) => (
              <button
                key={id}
                type="button"
                aria-pressed={view === id}
                onClick={() => setView(id)}
                className={cn(
                  "inline-flex min-h-9 items-center gap-1.5 rounded-sm px-3 text-sm font-medium transition-colors",
                  view === id ? "bg-card text-foreground shadow-raised" : "text-muted-foreground hover:text-foreground",
                )}
                data-testid={`steps-view-${id}`}
              >
                <Icon className="size-4" aria-hidden="true" />
                {t(id === "board" ? "viewBoard" : "viewList")}
              </button>
            ))}
          </div>
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
      ) : (
        <StepTable steps={shown} context={context} />
      )}
    </section>
  );
}
