"use client";

import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo } from "react";

import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";
import { type PlanStep, STEP_GROUPS, stepLabel } from "@/lib/plans";
import { cn } from "@/lib/utils";

import { DependsOn, formatDay, type StepContext as Context } from "./depends-on";
import { stepHref } from "./links";
import { BlockingBadge, StepStatusBadge } from "./status";

/**
 * The List view: the plan's steps in plan order, sortable. Its own module so step-board.tsx stays
 * under the 400-line budget.
 */
export default function StepTable({ steps, context }: { steps: PlanStep[]; context: Context }) {
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
      // On a phone: the step's title opening its page, its status, and its key with the repo it changes.
      mobile={(step) => ({
        title: stepLabel(step) || step.key,
        titleText: stepLabel(step) || step.key,
        href: stepHref(context.project, context.planId, step.key),
        tags: step.group === "done" ? null : <BlockingBadge blocking={step.blocking} />,
        status: <StepStatusBadge group={step.group} raw={step.rawStatus} />,
        meta: step.repo ? t("mobileMetaRepo", { key: step.key, repo: step.repo }) : t("mobileMeta", { key: step.key }),
        data: { "step-key": step.key },
      })}
    />
  );
}
