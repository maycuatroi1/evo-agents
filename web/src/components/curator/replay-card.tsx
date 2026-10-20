"use client";

import { useQuery } from "@tanstack/react-query";
import { CircleCheck, CircleDashed, CircleX, FlaskConical, Trophy } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { projectHref } from "@/components/shell/nav";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import { changeReplayQuery, type Replay } from "./insight-queries";
import { useInsightFormat } from "./insight-format";
import { NUMBER, Panel, PanelLine, PanelSkeleton, PanelTable, TABLE_CELL, TABLE_HEAD } from "./panel";
import type { Change } from "./queries";
import { armPassed, armTotals, lowered, type Verdict, verdictOf } from "./replay-model";

/** One arm of one replay in words and an icon: passed, failed, or not reported yet. */
function ArmResult({ passed, arm }: { passed: boolean | null; arm: "a" | "b" }) {
  const t = useTranslations("curator.replay");
  const Icon = passed === null ? CircleDashed : passed ? CircleCheck : CircleX;
  return (
    <span className={cn("inline-flex items-center gap-1 text-xs", passed === null ? "text-fg-subtle" : passed ? "text-success" : "text-danger")} data-arm={arm} data-passed={passed ?? undefined}>
      <span className="font-medium text-foreground" title={t(`arm.${arm}`)}>
        {t(`armHead.${arm}`)}
      </span>
      <Icon className="size-3.5 shrink-0" aria-hidden="true" />
      {passed === null ? t("pending") : passed ? t("passed") : t("failed")}
    </span>
  );
}

function ReplayLine({ project, replay }: { project: string; replay: Replay }) {
  const t = useTranslations("curator.replay");
  return (
    <li className="flex min-w-0 flex-col gap-1 px-4 py-2.5 text-[13px]" data-testid="replay" data-split={replay.split} data-state={replay.state}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <span className="min-w-0 font-medium text-pretty text-foreground [overflow-wrap:anywhere]">{replay.title ?? `${replay.plan_id} #${replay.step_key}`}</span>
        <Tag>{t(`split.${replay.split}`)}</Tag>
      </div>
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <span className="font-mono">
          {replay.plan_id} #{replay.step_key}
        </span>
        <ArmResult passed={armPassed(replay, "a")} arm="a" />
        <ArmResult passed={armPassed(replay, "b")} arm="b" />
        {replay.state === "failed" ? <span className="text-danger">{t("replayFailed")}</span> : null}
        {replay.run_id !== null ? (
          <Link href={projectHref(project, `runs/${replay.run_id}`) as Route} className="font-mono tabular-nums hover:text-brand" aria-label={t("openRun", { id: replay.run_id })}>
            #{replay.run_id}
          </Link>
        ) : null}
      </div>
      {replay.reason ? <p className="text-xs text-pretty text-fg-subtle [overflow-wrap:anywhere]">{replay.reason}</p> : null}
    </li>
  );
}

/** Arm a against arm b: tasks passed and the means of the verdict, then what the arms added up to. */
function ArmsTable({ verdict, arms }: { verdict: Verdict; arms: Parameters<typeof armTotals>[0] }) {
  const t = useTranslations("curator.replay");
  const format = useInsightFormat();
  const a = armTotals(arms, "a");
  const b = armTotals(arms, "b");
  const value = (held: number | null, show: (value: number) => string) => (held === null ? <span className="text-fg-subtle">-</span> : show(held));
  const saving = (share: number | null) =>
    share === null ? null : (
      <span className={cn("block text-xs", share > 0 ? "text-success" : share < 0 ? "text-danger" : "text-fg-subtle")}>
        {t(share >= 0 ? "lower" : "higher", { share: format.percent(Math.abs(share)) })}
      </span>
    );
  const rows = [
    { key: "passed", label: t("rows.passed", { tasks: verdict.tasks }), a: format.count(verdict.a.passed), b: format.count(verdict.b.passed), extra: null },
    { key: "cost", label: t("rows.cost"), a: value(verdict.a.cost, format.money), b: value(verdict.b.cost, format.money), extra: saving(verdict.saving.cost) },
    { key: "turns", label: t("rows.turns"), a: value(verdict.a.turns, format.decimal), b: value(verdict.b.turns, format.decimal), extra: saving(verdict.saving.turns) },
    { key: "errors", label: t("rows.errors"), a: value(a.toolErrors, format.count), b: value(b.toolErrors, format.count), extra: null },
    { key: "time", label: t("rows.time"), a: value(a.durationMs, format.ms), b: value(b.durationMs, format.ms), extra: null },
    { key: "peak", label: t("rows.peak"), a: value(a.peak, format.count), b: value(b.peak, format.count), extra: null },
  ];
  return (
    <PanelTable label={t("armsCaption")} testId="replay-arms">
      <thead className="border-b bg-surface-sunken">
        <tr>
          <th scope="col" className={TABLE_HEAD}>
            {t("measure")}
          </th>
          <th scope="col" className={cn(TABLE_HEAD, "text-right")} title={t("arm.a")}>
            {t("armHead.a")}
          </th>
          <th scope="col" className={cn(TABLE_HEAD, "text-right")} title={t("arm.b")}>
            {t("armHead.b")}
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.key} className="border-b last:border-b-0" data-testid="replay-arm-row" data-key={row.key}>
            <th scope="row" className={cn(TABLE_CELL, "text-left font-normal whitespace-normal")}>
              {row.label}
            </th>
            <td className={cn(TABLE_CELL, NUMBER)}>{row.a}</td>
            <td className={cn(TABLE_CELL, NUMBER, "text-foreground")}>
              {row.b}
              {row.extra}
            </td>
          </tr>
        ))}
      </tbody>
    </PanelTable>
  );
}

/**
 * A change's replays on its proposal's page (and in the Inbox's sheet): whether arm b, the change's harness, beat arm
 * a, the default branch's, on the project's own holdout tasks at the head of its pull request, and why; each arm's
 * figures side by side; each replay with both arms' results; and, once they won, that the change merges at tier 0. A
 * change the night shift does not replay (one that is not of a skill, an AGENTS.md or a CLAUDE.md, or a charter
 * without a replay budget) says so in one line.
 */
export function ReplayCard({ project, change, proposalTier }: { project: string; change: Change; proposalTier: number }) {
  const t = useTranslations("curator.replay");
  const state = useQuery(changeReplayQuery(browserApi, project, change.id));
  let body;
  let sub: string | undefined;
  if (state.isPending) body = <PanelSkeleton rows={3} />;
  else if (state.isError) {
    body = (
      <div className="flex flex-wrap items-center gap-3 px-4 py-3 text-[13px] text-muted-foreground" role="alert">
        <span>{t("unreadable")}</span>
        <Button variant="outline" size="sm" onClick={() => void state.refetch()}>
          {t("retry")}
        </Button>
      </div>
    );
  } else if (state.data.replays.length === 0) {
    body = <PanelLine testId="replay-none">{t("none", { tasks: state.data.tasks })}</PanelLine>;
  } else {
    const verdict = verdictOf(state.data.verdict);
    sub = t("sub", { count: state.data.replays.length, tasks: state.data.tasks });
    body = (
      <div className="flex min-w-0 flex-col" data-testid="replay-body" data-won={verdict?.won ?? false}>
        {verdict ? (
          <div className="flex min-w-0 flex-col gap-2 border-b px-4 py-3">
            <div className="flex flex-wrap items-center gap-2">
              <Tag className={verdict.won ? "bg-success-soft text-success" : undefined} data-testid="replay-verdict">
                {verdict.won ? <Trophy aria-hidden="true" /> : <FlaskConical aria-hidden="true" />}
                {verdict.won ? t("won") : t("notWon")}
              </Tag>
              <span className="text-xs text-fg-subtle tabular-nums">{t("holdout", { tasks: verdict.tasks, needed: verdict.needed })}</span>
            </div>
            {verdict.reason ? <p className="text-[13px] text-pretty text-muted-foreground [overflow-wrap:anywhere]" data-testid="replay-reason">{verdict.reason}</p> : null}
            {lowered(change.tier, proposalTier, verdict) ? (
              <p className="rounded-sm bg-success-soft px-2.5 py-1.5 text-[13px] text-success" data-testid="replay-tier">
                {t("tierZero", { tier: proposalTier })}
              </p>
            ) : null}
          </div>
        ) : null}
        {verdict ? <p className="border-b px-4 py-2 text-xs text-pretty text-fg-subtle">{t("armsNote")}</p> : null}
        {verdict ? <ArmsTable verdict={verdict} arms={state.data.arms} /> : null}
        <ul className="flex flex-col divide-y border-t" aria-label={t("tasks")} data-testid="replay-list">
          {state.data.replays.map((replay) => (
            <ReplayLine key={replay.id} project={project} replay={replay} />
          ))}
        </ul>
      </div>
    );
  }
  return (
    <Panel title={t("title")} sub={sub} testId="proposal-replay">
      {body}
    </Panel>
  );
}
