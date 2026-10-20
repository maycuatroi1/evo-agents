"use client";

import { useTranslations } from "next-intl";
import { useMemo } from "react";

import type { DataListRow } from "@/components/data/data-list";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { useMoney } from "@/components/data/facts";
import { Identifier, RunRef, Tag } from "@/components/data/identifier";
import { useIsMobile } from "@/hooks/use-mobile";

import { CiBadge, RunStateBadge } from "./badges";
import { failedShown, githubTagUrl, hasTagDetail, safeUrl } from "./model";
import { OutLink, Relative } from "./parts";
import { type ReleaseCycle, type ReleaseCycleRun, type ReleaseTag, runHref } from "./queries";

/** What a tag failed, under its row: the test ids (or failed jobs and steps), then what kept the hub from reading it. */
function TagDetail({ tag }: { tag: ReleaseTag }) {
  const t = useTranslations("releases.tags");
  const { shown, more } = failedShown(tag);
  return (
    <div className="flex min-w-0 flex-col gap-1.5 text-xs leading-4" data-testid="release-tag-detail">
      {shown.length > 0 ? (
        <div className="flex min-w-0 flex-col gap-1">
          <p className="text-muted-foreground">{t("failedTitle", { count: tag.failed_tests.length })}</p>
          <ul className="flex min-w-0 flex-col gap-0.5" data-testid="release-failed-tests">
            {shown.map((id) => (
              <li key={id} className="font-mono [overflow-wrap:anywhere] text-danger">
                {id}
              </li>
            ))}
          </ul>
          {more > 0 ? <p className="text-fg-subtle">{t("failedMore", { count: more })}</p> : null}
        </div>
      ) : null}
      {tag.infra ? <p className="text-muted-foreground">{t("infra", { reruns: tag.reruns })}</p> : null}
      {tag.problem ? (
        <p className="text-danger [overflow-wrap:anywhere]" data-testid="release-tag-problem">
          {t("problem", { problem: tag.problem })}
        </p>
      ) : null}
    </div>
  );
}

/** The tags a cycle follows, in the order it began to, each with its CI, its workflow run and what failed. */
export function TagsTable({ project, cycle, repo }: { project: string; cycle: ReleaseCycle; repo: string | null }) {
  const t = useTranslations("releases.tags");
  const phone = useIsMobile(); // a phone lists the tags as rows, so what failed follows the list
  const columns = useMemo(() => {
    const helper = dataTableColumns<ReleaseTag>();
    return helper.columns([
      helper.display({
        id: "tag",
        header: () => t("columns.tag"),
        meta: { primary: true },
        cell: (info) => {
          const tag = info.row.original;
          return (
            <CellMain>
              <Identifier
                value={tag.tag}
                externalHref={githubTagUrl(repo, tag.tag)}
                linkLabel={t("tagLink", { tag: tag.tag })}
                testId="release-tag"
              />
            </CellMain>
          );
        },
      }),
      helper.display({ id: "ci", header: () => t("columns.ci"), cell: (info) => <CiBadge state={info.row.original.ci_state} /> }),
      helper.display({
        id: "workflow",
        header: () => t("columns.workflow"),
        cell: (info) => {
          const tag = info.row.original;
          if (!safeUrl(tag.workflow_url)) return <span className="text-fg-subtle">{t("noWorkflow")}</span>;
          return (
            <OutLink href={tag.workflow_url} testId="release-workflow-link">
              {tag.run_attempt && tag.run_attempt > 1 ? t("workflowAttempt", { attempt: tag.run_attempt }) : t("workflow")}
            </OutLink>
          );
        },
      }),
      helper.display({
        id: "made",
        header: () => t("columns.made"),
        cell: (info) => {
          const runId = info.row.original.run_id;
          if (runId === null) return <span className="text-muted-foreground">{t("byPerson")}</span>;
          return <RunRef id={runId} href={runHref(project, runId)} label={t("openRun", { id: runId })} />;
        },
      }),
      helper.display({
        id: "checked",
        header: () => t("columns.checked"),
        meta: { numeric: true },
        cell: (info) => {
          const checked = info.row.original.checked_at;
          return checked ? <Relative value={checked} /> : <span className="text-fg-subtle">{t("notChecked")}</span>;
        },
      }),
    ]);
  }, [t, project, repo]);

  const mobile = (tag: ReleaseTag): DataListRow => {
    const meta = tag.failed_tests.length > 0 ? t("failedCount", { count: tag.failed_tests.length }) : tag.run_id === null ? t("byPerson") : t("byRun", { id: tag.run_id });
    return {
      title: tag.tag,
      titleClassName: "font-mono",
      status: <CiBadge state={tag.ci_state} />,
      meta,
      metaText: meta,
      danger: tag.failed_tests.length > 0 || tag.problem !== null,
      actions: safeUrl(tag.workflow_url) ? <OutLink href={tag.workflow_url}>{t("workflow")}</OutLink> : undefined,
      data: { tag: tag.tag },
    };
  };

  const detailed = cycle.tags.filter(hasTagDetail);
  return (
    <>
      <DataTable
        data={cycle.tags}
        columns={columns}
        caption={t("caption", { id: cycle.id })}
        getRowId={(row) => String(row.tag)}
        columnClassNames={{ made: "hidden lg:table-cell", checked: "hidden sm:table-cell" }}
        testId="release-tags"
        mobile={mobile}
        detail={(tag) => (hasTagDetail(tag) ? <TagDetail tag={tag} /> : null)}
      />
      {phone && detailed.length > 0 ? (
        <ul className="flex flex-col gap-3 border-t px-4 py-3" data-testid="release-tag-details">
          {detailed.map((tag) => (
            <li key={tag.tag} className="flex min-w-0 flex-col gap-1">
              <span className="font-mono text-xs font-medium text-foreground">{tag.tag}</span>
              <TagDetail tag={tag} />
            </li>
          ))}
        </ul>
      ) : null}
    </>
  );
}

/** How the hub landed a run: tagged with its pull request, refused or failing with the reason, or waiting. */
function Landing({ run }: { run: ReleaseCycleRun }) {
  const t = useTranslations("releases.runs");
  if (run.land_state === "tagged") {
    return (
      <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
        <span>{t("tagged", { tag: run.tagged ?? "" })}</span>
        {safeUrl(run.pull_url) ? (
          <OutLink href={run.pull_url} testId="release-pull-link">
            {t("pull")}
          </OutLink>
        ) : null}
      </span>
    );
  }
  if (run.land_state === "refused" || run.land_state === "failed") {
    return (
      <span className="text-danger [overflow-wrap:anywhere]" title={run.land_reason ?? undefined} data-testid="release-land-refused">
        {t(run.land_state, { reason: run.land_reason ?? "" })}
      </span>
    );
  }
  if (run.version) return <span className="text-muted-foreground">{t("waitingLand", { version: run.version })}</span>;
  return <span className="text-fg-subtle">{t("notPushed")}</span>;
}

/** The runs of a cycle, oldest first: a cut, then the fixes of red tags, each with the version it pushed and how it landed. */
export function RunsTable({ project, cycle }: { project: string; cycle: ReleaseCycle }) {
  const t = useTranslations("releases.runs");
  const money = useMoney();
  const columns = useMemo(() => {
    const helper = dataTableColumns<ReleaseCycleRun>();
    return helper.columns([
      helper.display({
        id: "run",
        header: () => t("columns.run"),
        meta: { primary: true },
        cell: (info) => {
          const run = info.row.original;
          return (
            <CellMain sub={run.version ? t("version", { version: run.version }) : undefined}>
              <RunRef id={run.run_id} href={runHref(project, run.run_id)} label={t("openRun", { id: run.run_id })} data-testid="release-run-link" />
              <Tag data-testid="release-run-mode">{run.mode === "fix" && run.tag ? t("fixOf", { tag: run.tag }) : t(`mode.${run.mode}`)}</Tag>
            </CellMain>
          );
        },
      }),
      helper.display({ id: "state", header: () => t("columns.state"), cell: (info) => <RunStateBadge state={info.row.original.state} /> }),
      helper.display({ id: "landing", header: () => t("columns.landing"), cell: (info) => <Landing run={info.row.original} /> }),
      helper.display({
        id: "cost",
        header: () => t("columns.cost"),
        meta: { numeric: true },
        cell: (info) => <span className="tabular-nums">{money(info.row.original.cost_usd)}</span>,
      }),
    ]);
  }, [t, project, money]);

  const mobile = (run: ReleaseCycleRun): DataListRow => {
    const mode = run.mode === "fix" && run.tag ? t("fixOf", { tag: run.tag }) : t(`mode.${run.mode}`);
    const meta = [mode, run.version ? t("version", { version: run.version }) : null, money(run.cost_usd)].filter(Boolean).join(", ");
    return {
      title: `#${run.run_id}`,
      titleClassName: "font-mono",
      href: runHref(project, run.run_id),
      status: <RunStateBadge state={run.state} />,
      meta,
      metaText: meta,
      danger: run.land_state === "refused" || run.land_state === "failed",
      data: { "run-id": run.run_id },
    };
  };

  return (
    <DataTable
      data={cycle.runs}
      columns={columns}
      caption={t("caption", { id: cycle.id })}
      getRowId={(row) => String(row.run_id)}
      columnClassNames={{ landing: "hidden md:table-cell" }}
      testId="release-runs"
      mobile={mobile}
    />
  );
}
