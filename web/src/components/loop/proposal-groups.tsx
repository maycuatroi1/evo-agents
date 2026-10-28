"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ArrowUpRight, ChevronDown, FlaskConical, Goal, Lightbulb, Sparkles, X } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { RunRef, Tag } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { browserApi } from "@/lib/api/browser";
import { type BoardItem, canvasReadsKey, goalsHref, loopKeys, rejectProposals, runPageHref } from "@/lib/loop-queries";
import { cn } from "@/lib/utils";

import { idsByCanvas, type ProposalGroup, type ProposalItem, type ProposalRun, proposalGroups } from "./model";

const MAX_REASON = 1_000; // tables_canvas.MAX_REASON_CHARS

/**
 * Proposals rejected together: one call per canvas they belong to, with one reason for all; then the board and the
 * reads of the canvases again. A refusal stops at the canvas it came from and is kept for the form.
 */
function useRejectTogether(project: string) {
  const t = useTranslations("loop.decide.proposals");
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const reject = async (items: readonly ProposalItem[], reason: string): Promise<boolean> => {
    setBusy(true);
    setRefused(null);
    let rejected = 0;
    let left = 0;
    try {
      for (const [slug, ids] of idsByCanvas(items)) {
        const done = await rejectProposals(browserApi(), project, slug, ids, reason);
        rejected += done.rejected.length;
        left += done.left.length;
      }
      notify({ tone: "success", text: left ? t("rejectedLeft", { count: rejected, left }) : t("rejected", { count: rejected }) });
      return true;
    } catch (error) {
      setRefused(failure(error));
      return false;
    } finally {
      setBusy(false);
      await Promise.all([queryClient.invalidateQueries({ queryKey: loopKeys.board(project) }), queryClient.invalidateQueries({ queryKey: canvasReadsKey(project) })]);
    }
  };
  return { busy, refused, reject };
}

/** The reason asked once for every proposal of a goal or of a run, and the button that rejects them. */
function RejectTogetherForm({ project, items, onClose }: { project: string; items: readonly ProposalItem[]; onClose: () => void }) {
  const t = useTranslations("loop.decide.proposals");
  const id = useId();
  const [reason, setReason] = useState("");
  const { busy, refused, reject } = useRejectTogether(project);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!reason.trim()) return;
    if (await reject(items, reason.trim())) onClose();
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-2 rounded-md border border-danger/30 bg-danger-soft/40 p-3" data-testid="proposals-reject-form">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <Label htmlFor={id}>{t("rejectReason", { count: items.length })}</Label>
          <Input id={id} value={reason} onChange={(event) => setReason(event.target.value)} maxLength={MAX_REASON} required autoFocus placeholder={t("rejectPlaceholder")} data-testid="proposals-reject-reason" />
        </div>
        <div className="flex gap-2">
          <Button type="button" variant="ghost" onClick={onClose} disabled={busy}>
            {t("cancel")}
          </Button>
          <Button type="submit" variant="destructive" busy={busy} disabled={!reason.trim()} data-testid="proposals-reject-confirm">
            {t("rejectConfirm", { count: items.length })}
          </Button>
        </div>
      </div>
      {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
    </form>
  );
}

/** One proposal: its cell and sentence, its test card, and the link to it on its canvas, where it is accepted. */
function ProposalLine({ item, reasonShown }: { item: ProposalItem; reasonShown: boolean }) {
  const t = useTranslations("loop.decide.proposals");
  const cells = useTranslations("canvas.cell");
  const statuses = useTranslations("canvas.status");
  const evidence = useTranslations("canvas.evidence");
  const { proposal } = item;
  const what = proposal.claim
    ? proposal.status
      ? t("statusTo", { claim: proposal.claim, status: statuses(proposal.status) })
      : t("change", { claim: proposal.claim, fields: proposal.fields.join(", ") })
    : t("newClaim");
  return (
    <li className="flex flex-wrap items-start gap-x-3 gap-y-1.5 py-2.5" data-testid="proposal-line" data-proposal={proposal.id} data-kind={proposal.kind}>
      <div className="flex min-w-0 flex-1 basis-72 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-1.5">
          {proposal.cell ? <Tag data-testid="proposal-cell">{cells(proposal.cell)}</Tag> : null}
          <Tag className={proposal.claim ? "font-mono" : undefined}>{what}</Tag>
          {proposal.synthetic ? (
            <Tag className="bg-review-soft text-review" title={evidence("syntheticTitle")}>
              <Sparkles aria-hidden="true" />
              {evidence("synthetic")}
            </Tag>
          ) : null}
        </div>
        {proposal.text ? (
          <p className="text-sm text-pretty text-foreground [overflow-wrap:anywhere]" data-testid="proposal-text">
            {proposal.text}
          </p>
        ) : null}
        {proposal.test ? (
          <p className="flex items-start gap-1.5 text-xs text-muted-foreground" data-testid="proposal-test">
            <FlaskConical className="mt-px size-3.5 shrink-0" aria-hidden="true" />
            <span className="sr-only">{t("test")}</span>
            <span className="font-mono [overflow-wrap:anywhere]">{proposal.test}</span>
          </p>
        ) : null}
        {reasonShown ? <p className="text-xs text-pretty text-fg-subtle [overflow-wrap:anywhere]">{t("why", { reason: proposal.reason })}</p> : null}
      </div>
      {item.link ? (
        <Button asChild variant="ghost" size="sm" className="shrink-0">
          <Link href={item.link as Route} aria-label={t("openLabel", { id: proposal.id, canvas: proposal.canvas })} data-testid="proposal-open">
            <span className="font-mono text-[12px] text-fg-subtle tabular-nums">#{proposal.id}</span>
            {t("open")}
            <ArrowUpRight aria-hidden="true" />
          </Link>
        </Button>
      ) : null}
    </li>
  );
}

/** The proposals one run made: which run, since when, the reason they share once, and rejecting them together. */
function RunBlock({ project, run, writer, alone }: { project: string; run: ProposalRun; writer: boolean; alone: boolean }) {
  const t = useTranslations("loop.decide.proposals");
  const [rejecting, setRejecting] = useState(false);
  return (
    <div className="flex flex-col border-t first:border-t-0" data-testid="proposal-run" data-run={run.run ?? ""}>
      <div className="flex min-h-9 flex-wrap items-center gap-x-2 gap-y-1 pt-2.5 text-xs text-fg-subtle">
        {run.run !== null ? (
          <span className="inline-flex items-center gap-1">
            {t("run")}
            <RunRef id={run.run} href={runPageHref(project, run.run)} label={t("runLabel", { id: run.run })} className="text-xs" />
          </span>
        ) : (
          <span>{t("noRun")}</span>
        )}
        {run.since ? <When value={run.since} short /> : null}
        <span aria-hidden="true">-</span>
        <span>{t("count", { count: run.items.length })}</span>
        {writer && !alone && !rejecting ? (
          <Button type="button" variant="quiet-danger" size="sm" className="ml-auto" onClick={() => setRejecting(true)} data-testid="proposal-run-reject">
            <X aria-hidden="true" />
            {t("rejectRun", { count: run.items.length })}
          </Button>
        ) : null}
      </div>
      {run.reason ? <p className="pt-1 text-xs text-pretty text-muted-foreground [overflow-wrap:anywhere]">{t("why", { reason: run.reason })}</p> : null}
      {rejecting ? (
        <div className="pt-2">
          <RejectTogetherForm project={project} items={run.items} onClose={() => setRejecting(false)} />
        </div>
      ) : null}
      <ul className="divide-y divide-border/60">
        {run.items.map((item) => (
          <ProposalLine key={item.proposal.id} item={item} reasonShown={run.reason === null} />
        ))}
      </ul>
    </div>
  );
}

/** The proposals of the brainstorms of one goal (or of no goal): the goal as it reads now, then its runs. */
function GoalGroup({ project, group, writer }: { project: string; group: ProposalGroup; writer: boolean }) {
  const t = useTranslations("loop.decide.proposals");
  const bodyId = useId();
  const [open, setOpen] = useState(true);
  const [rejecting, setRejecting] = useState(false);
  const name = group.goal?.code ?? t("noGoal");
  return (
    <li className="flex flex-col px-4 py-3" data-testid="proposal-group" data-goal={group.goal?.code ?? ""}>
      <div className="flex flex-wrap items-start gap-x-3 gap-y-2">
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          className="-ml-1 shrink-0 aria-expanded:bg-transparent aria-expanded:text-muted-foreground aria-expanded:hover:bg-accent aria-expanded:hover:text-foreground"
          aria-expanded={open}
          aria-controls={bodyId}
          aria-label={open ? t("collapse", { name }) : t("expand", { name })}
          onClick={() => setOpen(!open)}
          data-testid="proposal-group-toggle"
        >
          <ChevronDown className={cn("transition-transform duration-200 motion-reduce:transition-none", !open && "-rotate-90")} aria-hidden="true" />
        </Button>
        <div className="flex min-w-0 flex-1 basis-64 flex-col gap-1">
          <div className="flex flex-wrap items-center gap-2">
            {group.goal ? (
              <Link href={goalsHref(project, group.goal.code)} className="rounded-xs focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none" data-testid="proposal-group-goal">
                <Tag className="bg-brand-soft text-brand">
                  <Goal aria-hidden="true" />
                  {group.goal.code}
                </Tag>
              </Link>
            ) : (
              <Tag>
                <Lightbulb aria-hidden="true" />
                {t("noGoal")}
              </Tag>
            )}
            <span className="text-xs text-fg-subtle">
              {t("count", { count: group.count })}, {t("runs", { count: group.runs.length })}
            </span>
          </div>
          {group.goal ? (
            <>
              <p className="text-sm font-medium text-pretty text-foreground [overflow-wrap:anywhere]">{group.goal.statement}</p>
              <p className="text-xs text-muted-foreground [overflow-wrap:anywhere]">{t("metric", { metric: group.goal.metric })}</p>
            </>
          ) : (
            <p className="text-[13px] text-pretty text-muted-foreground">{t("noGoalText")}</p>
          )}
        </div>
        {writer && !rejecting ? (
          <Button type="button" variant="quiet-danger" size="sm" className="shrink-0 max-sm:ml-9" onClick={() => setRejecting(true)} data-testid="proposal-group-reject">
            <X aria-hidden="true" />
            {t("rejectAll", { count: group.count })}
          </Button>
        ) : null}
      </div>
      {rejecting ? (
        <div className="pt-3 sm:pl-9">
          <RejectTogetherForm project={project} items={group.runs.flatMap((run) => run.items)} onClose={() => setRejecting(false)} />
        </div>
      ) : null}
      <div id={bodyId} hidden={!open} className="sm:pl-9">
        {group.runs.map((run) => (
          <RunBlock key={run.run ?? "none"} project={project} run={run} writer={writer} alone={group.runs.length === 1} />
        ))}
      </div>
    </li>
  );
}

/**
 * The proposals of claims on the Decisions page, read in place: by the goal whose brainstorm by the Loop made them,
 * then by run, each with its cell, its sentence and its test card. Who may decide them is said once; a writer rejects
 * a goal's or a run's proposals together with one reason, and opens one on its canvas to accept it.
 */
export function ProposalGroups({ project, items, login }: { project: string; items: readonly BoardItem[]; login: string | null }) {
  const t = useTranslations("loop.decide");
  const decidedBy = useTranslations("loop.decidedBy");
  const groups = proposalGroups(items);
  if (groups.length === 0) return null;
  const count = groups.reduce((sum, group) => sum + group.count, 0);
  const deciders = groups[0]?.runs[0]?.items[0]?.deciders ?? [];
  const writer = login !== null && deciders.includes(login);
  return (
    <div className="border-t first:border-t-0" data-testid="board-proposals">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 bg-surface-sunken/60 px-4 py-2 text-xs text-fg-subtle">
        <span className="font-medium text-muted-foreground">{t("proposals.summary", { count })}</span>
        <span data-testid="board-proposals-deciders">{deciders.length > 0 ? t("deciders", { rule: decidedBy("writer"), logins: deciders.join(", ") }) : t("nobody", { rule: decidedBy("writer") })}</span>
        {writer ? (
          <Tag className="bg-attention-soft text-attention" data-testid="board-proposals-mine">
            {t("yours")}
          </Tag>
        ) : null}
      </div>
      <ul className="divide-y">
        {groups.map((group) => (
          <GoalGroup key={group.key} project={project} group={group} writer={writer} />
        ))}
      </ul>
    </div>
  );
}
