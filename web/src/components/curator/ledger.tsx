"use client";

import { ArrowRight, Bot, CircleCheck, CircleHelp, CircleX, ExternalLink, type LucideIcon, MoonStar, Undo2, User } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Identifier, RunRef, Tag } from "@/components/data/identifier";
import { HomeCard } from "@/components/home/parts";
import { runHref } from "@/components/runs/queries";
import { useHubQuery } from "@/components/states/query-view";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Ago } from "@/components/workers/ago";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import {
  isMoney,
  type LedgerTone,
  ledgerTone,
  outcomeFigures,
  revertProposal,
  safeUrl,
  shortSha,
  triggerFigures,
} from "./ledger-model";
import { TEXT_LINK, useMoney } from "./parts";
import { type LedgerLine, ledgerQuery, proposalHref } from "./queries";

/**
 * A proposal's ledger (web/DESIGN.md, components/curator): what happened to it, line by line, oldest first, as the hub
 * added the lines and never changed them. Each line is a mark in the tone of what it says, what happened, who did it
 * (the Curator's own code, an agent with its run, or a member), when, the commits, the pull request, the Judge's
 * verdict, and the figures: those that set the proposal off on its first line, and before and after the merge on its
 * outcome. An ordered list, so a screen reader hears the order.
 */

const MARK: Record<LedgerTone, string> = {
  success: "bg-success-solid",
  danger: "bg-danger-solid",
  attention: "bg-attention-solid",
  neutral: "bg-neutral-solid",
};

const ACTOR_ICON: Record<LedgerLine["actor"], LucideIcon> = { curator: MoonStar, agent: Bot, user: User };
const OUTCOME_ICON: Record<NonNullable<LedgerLine["outcome"]>, LucideIcon> = { keep: CircleCheck, revert: Undo2, unclear: CircleHelp };

function ActorTag({ line }: { line: LedgerLine }) {
  const t = useTranslations("curator.ledger");
  const Icon = ACTOR_ICON[line.actor];
  return (
    <Tag data-testid="ledger-actor" data-actor={line.actor}>
      <Icon aria-hidden="true" />
      <span className="sr-only">{t("by")} </span>
      {line.actor === "user" && line.actor_login ? line.actor_login : t(`actor.${line.actor}`)}
    </Tag>
  );
}

/** The commit a line is about, and the default branch before and after it, each a short hash that copies whole. */
function Commits({ line }: { line: LedgerLine }) {
  const t = useTranslations("curator.ledger");
  if (!line.commit_sha && !line.before_sha && !line.after_sha) return null;
  const chip = (sha: string, testId: string) => (
    <Identifier value={sha} title={sha} copy copyLabel={t("copyCommit", { sha })} testId={testId}>
      {shortSha(sha)}
    </Identifier>
  );
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-muted-foreground">
      {line.commit_sha ? (
        <span className="inline-flex min-w-0 items-center gap-1.5">
          {t("commit")} {chip(line.commit_sha, "ledger-commit")}
        </span>
      ) : null}
      {line.before_sha || line.after_sha ? (
        <span className="inline-flex min-w-0 flex-wrap items-center gap-1.5" data-testid="ledger-branch">
          {t("branch")}
          {line.before_sha ? chip(line.before_sha, "ledger-before") : <span>-</span>}
          <ArrowRight className="size-3.5 shrink-0" aria-hidden="true" />
          <span className="sr-only">{t("then")}</span>
          {line.after_sha ? chip(line.after_sha, "ledger-after") : <span>-</span>}
        </span>
      ) : null}
    </div>
  );
}

function Verdict({ line }: { line: LedgerLine }) {
  const t = useTranslations("curator.ledger");
  const verdict = line.verdict as { passed?: unknown; failures?: unknown } | null;
  if (line.action !== "judged" || !verdict) return null;
  const passed = verdict.passed === true;
  const failures = Array.isArray(verdict.failures) ? verdict.failures.filter((item): item is string => typeof item === "string") : [];
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <Tag className={passed ? "bg-success-soft text-success" : "bg-danger-soft text-danger"} data-testid="ledger-verdict" data-passed={passed}>
        {passed ? <CircleCheck aria-hidden="true" /> : <CircleX aria-hidden="true" />}
        {passed ? t("passed") : t("failed")}
      </Tag>
      {failures.length > 0 ? (
        <ul className="flex list-disc flex-col gap-0.5 pl-5 text-[13px] text-muted-foreground">
          {failures.map((failure, index) => (
            <li key={index} className="[overflow-wrap:anywhere]">
              {failure}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function useFigureText() {
  const format = useFormatter();
  const money = useMoney();
  return (key: string, value: number) => (isMoney(key) ? money(value) : format.number(value, { maximumFractionDigits: 2 }));
}

/** The figures that set the proposal off, on its first line. */
function TriggerTable({ line }: { line: LedgerLine }) {
  const t = useTranslations("curator.ledger");
  const figure = useFigureText();
  const { activity, figures } = triggerFigures(line.figures);
  if (line.action !== "proposed" || line.figures === null) return null;
  if (figures.length === 0) {
    return <p className="text-[13px] text-muted-foreground" data-testid="ledger-figures-none">{t("triggerNone")}</p>;
  }
  return (
    <div className="flex min-w-0 flex-col gap-1.5" data-testid="ledger-figures">
      <h4 className="text-xs font-medium text-muted-foreground">
        {t("trigger")}
        {activity !== null ? <span className="font-normal text-fg-subtle">, {t("activity", { count: activity })}</span> : null}
      </h4>
      <Table scrollLabel={t("figuresOf", { id: line.id })} className="text-[13px]">
        <TableHeader>
          <TableRow>
            <TableHead>{t("figure")}</TableHead>
            <TableHead className="text-right">{t("count")}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {figures.map((item) => (
            <TableRow key={item.key} data-testid="ledger-figure" data-key={item.key}>
              <TableCell className="whitespace-normal [overflow-wrap:anywhere]">{item.what}</TableCell>
              <TableCell className="text-right tabular-nums">{figure(item.key, item.value)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

const CHANGE_LOOK = {
  better: "text-success",
  same: "text-muted-foreground",
  worse: "text-danger",
} as const;

/** The outcome of a merged change: kept, to revert or unclear, and each figure before and after the merge. */
function Outcome({ project, line }: { project: string; line: LedgerLine }) {
  const t = useTranslations("curator.ledger");
  const figure = useFigureText();
  const format = useFormatter();
  if (line.action !== "outcome" || !line.outcome) return null;
  const Icon = OUTCOME_ICON[line.outcome];
  const found = outcomeFigures(line.figures);
  const revert = revertProposal(line);
  const tone = line.outcome === "keep" ? "bg-success-soft text-success" : line.outcome === "revert" ? "bg-danger-soft text-danger" : "";
  const rate = (value: number) => format.number(value, { maximumFractionDigits: 2 });
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Tag className={tone} data-testid="ledger-outcome" data-outcome={line.outcome}>
          <Icon aria-hidden="true" />
          {t(`outcome.${line.outcome}`)}
        </Tag>
        {revert !== null ? (
          <span className="text-[13px]">
            {t.rich("revertProposed", {
              id: revert,
              link: (chunks) => (
                <Link href={proposalHref(project, revert)} className={TEXT_LINK} data-testid="ledger-revert-link">
                  {chunks}
                </Link>
              ),
            })}
          </span>
        ) : null}
      </div>
      {found.figures.length > 0 ? (
        <div className="flex min-w-0 flex-col gap-1.5" data-testid="ledger-outcome-figures">
          {found.before !== null && found.after !== null ? (
            <h4 className="text-xs font-medium text-muted-foreground">
              {t("counted", { before: found.before, after: found.after })}
            </h4>
          ) : null}
          <Table scrollLabel={t("figuresOf", { id: line.id })} className="text-[13px]">
            <TableHeader>
              <TableRow>
                <TableHead>{t("figure")}</TableHead>
                <TableHead className="text-right">{t("before")}</TableHead>
                <TableHead className="text-right">{t("after")}</TableHead>
                <TableHead className="text-right">{t("rateBefore")}</TableHead>
                <TableHead className="text-right">{t("rateAfter")}</TableHead>
                <TableHead>{t("change")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {found.figures.map((item) => (
                <TableRow key={item.key} data-testid="ledger-outcome-figure" data-key={item.key} data-change={item.change}>
                  <TableCell className="min-w-40 whitespace-normal [overflow-wrap:anywhere]">{item.what}</TableCell>
                  <TableCell className="text-right tabular-nums">{figure(item.key, item.value)}</TableCell>
                  <TableCell className="text-right tabular-nums">{figure(item.key, item.after)}</TableCell>
                  <TableCell className="text-right tabular-nums">{rate(item.beforeRate)}</TableCell>
                  <TableCell className="text-right tabular-nums">{rate(item.afterRate)}</TableCell>
                  <TableCell className={cn("font-medium", CHANGE_LOOK[item.change])}>{t(`changes.${item.change}`)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      ) : null}
    </div>
  );
}

function PullLink({ line }: { line: LedgerLine }) {
  const t = useTranslations("curator.ledger");
  const url = safeUrl(line.pr_url);
  if (!url || !["pull_opened", "merged", "closed"].includes(line.action)) return null;
  return (
    <a href={url} target="_blank" rel="noreferrer noopener" className={cn(TEXT_LINK, "inline-flex w-fit items-center gap-1 text-[13px]")} data-testid="ledger-pr">
      {line.pr_number ? t("pull", { number: line.pr_number }) : t("pullPlain")}
      <ExternalLink className="size-3.5 shrink-0" aria-hidden="true" />
      <span className="sr-only">{t("newTab")}</span>
    </a>
  );
}

function Line({ project, line, last }: { project: string; line: LedgerLine; last: boolean }) {
  const t = useTranslations("curator.ledger");
  const tone = ledgerTone(line);
  const body: ReactNode[] = [
    <Commits key="commits" line={line} />,
    <PullLink key="pull" line={line} />,
    <Verdict key="verdict" line={line} />,
    <TriggerTable key="trigger" line={line} />,
    <Outcome key="outcome" project={project} line={line} />,
  ];
  return (
    <li className="relative flex min-w-0 gap-3 pb-5 last:pb-0" data-testid="ledger-line" data-action={line.action} data-actor={line.actor} data-tone={tone}>
      <div className="relative flex w-3 shrink-0 justify-center" aria-hidden="true">
        <span className={cn("relative z-[1] mt-1.5 size-2.5 rounded-full ring-4 ring-card", MARK[tone])} />
        {!last ? <span className="absolute top-4 -bottom-5 w-px bg-border" /> : null}
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-sm font-medium" data-testid="ledger-action">
            {t(`action.${line.action}`)}
          </span>
          <ActorTag line={line} />
          {line.run_id !== null ? (
            <RunRef id={line.run_id} href={runHref(project, line.run_id)} label={t("openRun", { id: line.run_id })} data-testid="ledger-run" />
          ) : null}
          <span className="ml-auto text-xs text-fg-subtle">
            <Ago value={line.created_at} never="-" />
          </span>
        </div>
        <p className="text-[13px] text-pretty text-muted-foreground [overflow-wrap:anywhere]" data-testid="ledger-what">
          {line.what}
        </p>
        {body}
      </div>
    </li>
  );
}

/** When the hub counts the figures of the merged change again: a moment ahead, as a date and time. */
function OutcomeDue({ at }: { at: string }) {
  const t = useTranslations("curator.ledger");
  const format = useFormatter();
  const date = new Date(at);
  return (
    <p className="text-[13px] text-muted-foreground" data-testid="ledger-outcome-due">
      {t.rich("outcomeDue", {
        at: () => (
          <time dateTime={at} className="whitespace-nowrap text-foreground tabular-nums">
            {format.dateTime(date, { dateStyle: "medium", timeStyle: "short" })}
          </time>
        ),
      })}
    </p>
  );
}

/** The ledger of a proposal, read with the proposal and again whenever it changes. */
export function LedgerCard({ project, id }: { project: string; id: number }) {
  const t = useTranslations("curator.ledger");
  const state = useHubQuery(ledgerQuery(browserApi, project, id), null, { live: true });
  let body: ReactNode;
  if (state.status === "loading") {
    body = (
      <div className="flex flex-col gap-3 px-4 py-4" aria-hidden="true">
        <Skeleton className="h-3 w-1/2" />
        <Skeleton className="h-3 w-2/3" />
        <Skeleton className="h-3 w-1/3" />
      </div>
    );
  } else if (state.status === "error") {
    body = (
      <div className="flex flex-wrap items-center gap-3 px-4 py-3 text-[13px] text-muted-foreground" role="alert">
        <span>{t("unreadable")}</span>
        <Button variant="outline" size="sm" onClick={state.retry}>
          {t("retry")}
        </Button>
      </div>
    );
  } else {
    const ledger = state.data;
    body = (
      <div className="flex flex-col gap-4 px-4 py-4">
        <p className="text-xs text-fg-subtle">{t("caption")}</p>
        {ledger.lines.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">{t("empty")}</p>
        ) : (
          <ol className="flex min-w-0 flex-col" aria-label={t("lines", { id })} data-testid="ledger-lines">
            {ledger.lines.map((line, index) => (
              <Line key={line.id} project={project} line={line} last={index === ledger.lines.length - 1} />
            ))}
          </ol>
        )}
        {ledger.outcome_due_at ? <OutcomeDue at={ledger.outcome_due_at} /> : null}
      </div>
    );
  }
  return (
    <HomeCard title={t("title")} testId="proposal-ledger">
      {body}
    </HomeCard>
  );
}
