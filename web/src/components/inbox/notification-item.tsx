"use client";

import { ArrowRight, Check, ChevronDown, GitBranch, GitCommitHorizontal, Lightbulb, MessageCircleQuestionMark } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type MouseEvent, useId, useState } from "react";

import { TierTag } from "@/components/curator/badges";
import { proposalHref } from "@/components/curator/queries";
import { Prose } from "@/components/plans/prose";
import { runHref } from "@/components/runs/queries";
import { StatusBadge } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { Ago } from "@/components/workers/ago";
import { cn } from "@/lib/utils";

import { NOTICE_LOOK, NoticeKindBadge } from "./badges";
import { COMMITS_SHOWN, isBranchNotice, isOpenProposal, isWaiting, noticeFacts, shortCommit } from "./model";
import type { Notification } from "./queries";

/** A body longer than this, or of more lines, folds to three lines with a button to show the rest. */
const FOLD_CHARS = 240;
const FOLD_LINES = 3;

/** Plain text the hub or an agent wrote (a notice's body): three lines first, the rest a click away. */
function FoldedText({ text }: { text: string }) {
  const t = useTranslations("inbox.item");
  const ids = useId();
  const [open, setOpen] = useState(false);
  const long = text.length > FOLD_CHARS || text.trim().split("\n").length > FOLD_LINES;
  return (
    <div className="flex flex-col items-start gap-1">
      <div id={`${ids}-body`} className={cn("w-full text-muted-foreground", long && !open && "line-clamp-3")} data-testid="notification-body">
        <Prose className="max-w-none">{text}</Prose>
      </div>
      {long ? (
        <Button
          type="button"
          variant="link"
          size="sm"
          className="h-auto px-0 text-xs"
          aria-expanded={open}
          aria-controls={`${ids}-body`}
          onClick={() => setOpen(!open)}
          data-testid="notification-body-toggle"
        >
          <ChevronDown className={cn("transition-transform duration-base motion-reduce:transition-none", open && "rotate-180")} aria-hidden="true" />
          {open ? t("showLess") : t("showMore")}
        </Button>
      ) : null}
    </div>
  );
}

/** The repo, branch and commits of a push or merge into a default branch. */
function BranchFacts({ notification }: { notification: Notification }) {
  const t = useTranslations("inbox.item");
  const facts = noticeFacts(notification.details);
  const [all, setAll] = useState(false);
  if (!facts.repo && !facts.branch && facts.commits.length === 0) return null;
  const shown = all ? facts.commits : facts.commits.slice(0, COMMITS_SHOWN);
  const more = facts.commits.length - shown.length;
  return (
    <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs" data-testid="notice-facts">
      {facts.repo ? (
        <>
          <dt className="text-muted-foreground">{t("repo")}</dt>
          <dd className="font-mono [overflow-wrap:anywhere]" data-testid="notice-repo">
            {facts.repo}
          </dd>
        </>
      ) : null}
      {facts.branch ? (
        <>
          <dt className="text-muted-foreground">{t("branch")}</dt>
          <dd className="flex min-w-0 items-center gap-1 font-mono [overflow-wrap:anywhere]" data-testid="notice-branch">
            <GitBranch className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
            {facts.branch}
          </dd>
        </>
      ) : null}
      {facts.commits.length > 0 ? (
        <>
          <dt className="text-muted-foreground">{t("commits", { count: facts.commits.length })}</dt>
          <dd className="min-w-0">
            <ul className="flex flex-wrap items-center gap-1.5" data-testid="notice-commits">
              {shown.map((sha) => (
                <li key={sha} className="inline-flex h-5 items-center gap-1 rounded-xs bg-surface-sunken px-1.5 font-mono text-muted-foreground" data-sha={sha}>
                  <GitCommitHorizontal className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                  <span title={sha}>{shortCommit(sha)}</span>
                  <span className="sr-only">{t("commitFull", { sha })}</span>
                </li>
              ))}
              {more > 0 || all ? (
                <li>
                  <Button type="button" variant="link" size="sm" className="h-auto px-0 text-xs" aria-expanded={all} onClick={() => setAll(!all)} data-testid="notice-commits-toggle">
                    {all ? t("fewerCommits") : t("moreCommits", { count: more })}
                  </Button>
                </li>
              ) : null}
            </ul>
          </dd>
        </>
      ) : null}
    </dl>
  );
}

/** Where a notice leads: the run's page, or the hub page its link names (never another site). */
function noticeHref(notification: Notification): Route | null {
  if (notification.project && notification.run_id !== null) return runHref(notification.project, notification.run_id);
  const link = notification.link ?? "";
  return link.startsWith("/") && !link.startsWith("//") ? (link as Route) : null;
}

/**
 * One notification of the Inbox. A decision opens in a sheet over the list (its title is a link to
 * `/inbox?decision=ID`, which `onOpenDecision` follows without leaving the page), and so does a tier 2 proposal of the
 * Curator (`/inbox?proposal=ID`, `onOpenProposal`); a notice links to its run. An unread one is marked by a dot, a
 * heavier title and the words "Unread" for screen readers, and offers Mark as read.
 */
export function NotificationItem({
  notification,
  href,
  selected,
  onOpenDecision,
  onOpenProposal,
  onRead,
  reading,
}: {
  notification: Notification;
  /** Where the title of a decision or a proposal leads: the Inbox with it open, the filters kept. */
  href: Route | null;
  selected: boolean;
  onOpenDecision: (id: number) => void;
  onOpenProposal?: (id: number) => void;
  onRead: (id: number) => void;
  reading: boolean;
}) {
  const t = useTranslations("inbox.item");
  const unread = notification.read_at === null;
  const decision = notification.kind === "decision";
  const proposal = notification.kind === "proposal" && notification.proposal_id != null;
  const asked = decision || proposal;
  const open = isWaiting(notification);
  const Icon = decision
    ? MessageCircleQuestionMark
    : proposal
      ? Lightbulb
      : notification.notice_kind
        ? NOTICE_LOOK[notification.notice_kind].icon
        : MessageCircleQuestionMark;
  const target = asked ? null : noticeHref(notification);
  const tier = typeof notification.details?.tier === "number" ? notification.details.tier : null;

  const follow = (event: MouseEvent<HTMLAnchorElement>) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (proposal && notification.proposal_id != null && onOpenProposal) {
      event.preventDefault();
      onOpenProposal(notification.proposal_id);
      return;
    }
    if (notification.decision_id === null) return;
    event.preventDefault();
    onOpenDecision(notification.decision_id);
  };

  return (
    <li
      className={cn(
        "relative flex min-w-0 gap-3 rounded-md border bg-card shadow-raised px-3.5 py-3 transition-colors sm:px-4",
        selected && "border-brand/50 bg-surface-selected ring-1 ring-brand/30",
        !selected && unread && "border-brand/25",
      )}
      aria-current={selected ? "true" : undefined}
      data-testid="notification"
      data-id={notification.id}
      data-kind={notification.kind}
      data-notice-kind={notification.notice_kind ?? undefined}
      data-decision-id={notification.decision_id ?? undefined}
      data-decision-state={notification.decision_state ?? undefined}
      data-proposal-id={notification.proposal_id ?? undefined}
      data-proposal-state={notification.proposal_state ?? undefined}
      data-unread={unread ? "true" : "false"}
    >
      <span
        className={cn(
          "mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-full",
          open ? "bg-attention-soft text-attention" : "bg-muted text-muted-foreground",
        )}
        aria-hidden="true"
      >
        <Icon className="size-4" />
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          {unread ? (
            <span className="inline-flex items-center gap-1.5 font-medium text-brand" data-testid="notification-unread">
              <span className="size-2 rounded-full bg-brand" aria-hidden="true" />
              {t("unread")}
            </span>
          ) : null}
          {decision && notification.decision_state ? <StatusBadge kind="decision" status={notification.decision_state} /> : null}
          {proposal && notification.proposal_state ? <StatusBadge kind="proposal" status={notification.proposal_state} /> : null}
          {proposal && tier !== null ? <TierTag tier={tier} /> : null}
          {!asked && notification.notice_kind ? <NoticeKindBadge kind={notification.notice_kind} /> : null}
          {notification.project ? <span className="font-mono [overflow-wrap:anywhere]">{notification.project}</span> : null}
          <Ago value={notification.created_at} never="-" />
        </div>
        {asked && href ? (
          <Link
            href={href}
            onClick={follow}
            className={cn(
              "text-sm text-pretty text-foreground underline-offset-4 [overflow-wrap:anywhere] hover:text-brand hover:underline",
              unread ? "font-semibold" : "font-medium",
            )}
            data-testid="notification-open"
            data-decision-link={decision ? (notification.decision_id ?? undefined) : undefined}
            data-proposal-link={proposal ? (notification.proposal_id ?? undefined) : undefined}
          >
            {notification.title}
          </Link>
        ) : (
          <p className={cn("text-sm text-pretty [overflow-wrap:anywhere]", unread ? "font-semibold" : "font-medium")} data-testid="notification-title">
            {notification.title}
          </p>
        )}
        {!asked && isBranchNotice(notification) ? <BranchFacts notification={notification} /> : null}
        {!decision && notification.body ? <FoldedText text={notification.body} /> : null}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 pt-0.5">
          {decision && href && notification.decision_id !== null ? (
            <Button asChild size="sm" variant={open ? "default" : "outline"}>
              <Link href={href} onClick={follow} data-testid="notification-answer" aria-label={t(open ? "answerLabel" : "viewLabel", { id: notification.decision_id })}>
                <MessageCircleQuestionMark aria-hidden="true" />
                {open ? t("answer") : t("view")}
              </Link>
            </Button>
          ) : null}
          {proposal && href && notification.proposal_id != null ? (
            <Button asChild size="sm" variant={isOpenProposal(notification) ? "default" : "outline"}>
              <Link
                href={href}
                onClick={follow}
                data-testid="notification-answer"
                aria-label={t(isOpenProposal(notification) ? "answerProposalLabel" : "viewProposalLabel", { id: notification.proposal_id })}
              >
                <Lightbulb aria-hidden="true" />
                {isOpenProposal(notification) ? t("answer") : t("view")}
              </Link>
            </Button>
          ) : null}
          {proposal && notification.project && notification.proposal_id != null ? (
            <Link
              href={proposalHref(notification.project, notification.proposal_id)}
              className="inline-flex min-h-7 items-center gap-1 text-sm text-brand underline-offset-4 hover:underline"
              data-testid="notification-proposal-link"
            >
              {t("openProposal", { id: notification.proposal_id })}
              <ArrowRight className="size-3.5" aria-hidden="true" />
            </Link>
          ) : null}
          {target && notification.run_id !== null ? (
            <Link
              href={target}
              onClick={() => {
                if (unread) onRead(notification.id);
              }}
              className="inline-flex min-h-7 items-center gap-1 text-sm font-medium text-brand underline-offset-4 hover:underline"
              data-testid="notification-run-link"
            >
              {t("openRun", { id: notification.run_id })}
              <ArrowRight className="size-3.5" aria-hidden="true" />
            </Link>
          ) : null}
          {decision && notification.run_id !== null && notification.project ? (
            <Link
              href={runHref(notification.project, notification.run_id)}
              className="inline-flex min-h-7 items-center gap-1 text-sm text-brand underline-offset-4 hover:underline"
              data-testid="notification-run-link"
            >
              {t("openRun", { id: notification.run_id })}
              <ArrowRight className="size-3.5" aria-hidden="true" />
            </Link>
          ) : null}
          {unread ? (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="ml-auto aria-disabled:opacity-60"
              busy={reading}
              aria-label={t("markReadLabel", { title: notification.title })}
              onClick={() => {
                if (!reading) onRead(notification.id);
              }}
              data-testid="notification-mark-read"
            >
              <Check aria-hidden="true" />
              {t("markRead")}
            </Button>
          ) : null}
        </div>
      </div>
    </li>
  );
}
