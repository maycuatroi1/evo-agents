"use client";

import { useQueries, useQuery } from "@tanstack/react-query";
import { CopyX, FileCode2, Info, Undo2 } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { Tag } from "@/components/data/identifier";
import { HomeCard } from "@/components/home/parts";
import { SafeMarkdown } from "@/components/memories/markdown";
import { runHref } from "@/components/runs/queries";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { NotFoundState, PageSkeleton } from "@/components/states/states";
import { StatusBadge } from "@/components/status/status-badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Ago } from "@/components/workers/ago";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { projectQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { AnswerButtons, AnswerDialog } from "./answer-dialog";
import { KindTag, LensTag, TierTag } from "./badges";
import { DraftPlan } from "./draft-plan";
import { EvidenceList } from "./evidence";
import { useCuratorViewer } from "./hooks";
import { LedgerCard } from "./ledger";
import type { RepoInfo } from "./model";
import { TEXT_LINK } from "./parts";
import { type Finding, findingQuery, type Proposal, type ProposalAction, proposalHref, proposalQuery } from "./queries";

/** The repos of the project, for links to code; none while they load or when the project cannot be read. */
function useRepos(project: string): RepoInfo[] {
  const { data } = useQuery(projectQuery(browserApi, project));
  return data?.repos ?? [];
}

function FindingItem({ project, finding, repos }: { project: string; finding: Finding; repos: RepoInfo[] }) {
  const t = useTranslations("curator.finding");
  return (
    <li className="flex min-w-0 flex-col gap-2 rounded-md border px-3 py-3" data-testid="proposal-finding" data-finding-id={finding.id}>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-muted-foreground tabular-nums">#{finding.id}</span>
        <span className="min-w-0 text-sm font-medium text-pretty [overflow-wrap:anywhere]">{finding.title}</span>
        <LensTag lens={finding.lens} />
        <Tag data-severity={finding.severity} className={cn(finding.severity === "high" && "text-foreground")}>
          {t(`severity.${finding.severity}`)}
        </Tag>
      </div>
      {finding.body ? <SafeMarkdown className="text-[13px]" testId="finding-body">{finding.body}</SafeMarkdown> : null}
      <EvidenceList project={project} evidence={finding.evidence as Record<string, unknown>[]} repos={repos} testId="finding-evidence" />
    </li>
  );
}

/** The findings a proposal rests on, each read on its own (the hub keeps them apart), in the order the proposal names them. */
function Findings({ project, ids, repos }: { project: string; ids: number[]; repos: RepoInfo[] }) {
  const t = useTranslations("curator.finding");
  const found = useQueries({ queries: ids.map((id) => findingQuery(browserApi, project, id)) });
  if (ids.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-[13px] font-medium text-muted-foreground">{t("title", { count: ids.length })}</h3>
      <ul className="flex flex-col gap-2.5" data-testid="proposal-findings">
        {found.map((query, index) =>
          query.data ? (
            <FindingItem key={ids[index]} project={project} finding={query.data} repos={repos} />
          ) : query.isError ? (
            <li key={ids[index]} className="rounded-md border px-3 py-2.5 text-[13px] text-muted-foreground">
              {t("unreadable", { id: ids[index] })}
            </li>
          ) : (
            <li key={ids[index]} className="rounded-md border px-3 py-3" aria-hidden="true">
              <Skeleton className="h-3 w-2/3" />
            </li>
          ),
        )}
      </ul>
    </div>
  );
}

function PathList({ paths, testId }: { paths: { repo: string; path: string }[]; testId: string }) {
  return (
    <ul className="flex flex-col gap-1" data-testid={testId}>
      {paths.map((item) => (
        <li key={`${item.repo}:${item.path}`} className="flex min-w-0 items-start gap-1.5 font-mono text-xs [overflow-wrap:anywhere]">
          <FileCode2 className="mt-px size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span>
            <span className="text-muted-foreground">{item.repo}:</span>
            {item.path}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** Why the hub gave the proposal its tier, rule by rule, and what it would change. */
function TierCard({ proposal }: { proposal: Proposal }) {
  const t = useTranslations("curator.proposal");
  const tTier = useTranslations("curator.tier");
  return (
    <HomeCard title={t("why", { tier: proposal.tier })} testId="proposal-tier-card">
      <div className="flex flex-col gap-4 px-4 py-3 text-[13px]">
        <p className="text-muted-foreground">{tTier(`meaning.${Math.min(3, Math.max(0, proposal.tier)) as 0 | 1 | 2 | 3}`)}</p>
        <ul className="flex list-disc flex-col gap-1 pl-5" data-testid="proposal-tier-reasons">
          {proposal.tier_reasons.map((reason, index) => (
            <li key={index} className="text-pretty [overflow-wrap:anywhere]">
              {reason}
            </li>
          ))}
        </ul>
        <div className="flex flex-col gap-1.5">
          <h3 className="text-xs font-medium text-muted-foreground">{t("touches", { count: proposal.paths.length })}</h3>
          {proposal.paths.length > 0 ? <PathList paths={proposal.paths} testId="proposal-paths" /> : <p className="text-muted-foreground">{t("noPaths")}</p>}
        </div>
        <div className="flex flex-col gap-1.5">
          <h3 className="text-xs font-medium text-muted-foreground">{t("impacted")}</h3>
          {proposal.impacted === null ? (
            <p className="text-muted-foreground">{t("noGraph")}</p>
          ) : proposal.impacted.length === 0 ? (
            <p className="text-muted-foreground">{t("nothingImpacted")}</p>
          ) : (
            <PathList paths={proposal.impacted} testId="proposal-impacted" />
          )}
        </div>
      </div>
    </HomeCard>
  );
}

/** The answer as the hub holds it: who answered, when and with what note; or that it waits, and for whom. */
function AnswerCard({ project, proposal, admin }: { project: string; proposal: Proposal; admin: boolean }) {
  const t = useTranslations("curator.proposal");
  let body: ReactNode;
  if (proposal.state === "dropped") {
    body = (
      <p className="text-pretty" data-testid="proposal-dropped">
        {proposal.duplicate_of !== null
          ? t.rich("dropped", {
              id: proposal.duplicate_of,
              link: (chunks) => (
                <Link href={proposalHref(project, proposal.duplicate_of ?? 0)} className={TEXT_LINK}>
                  {chunks}
                </Link>
              ),
            })
          : t("droppedPlain")}
      </p>
    );
  } else if (proposal.answered_by) {
    body = (
      <div className="flex flex-col gap-1.5">
        <p data-testid="proposal-answered-by">
          {t.rich("answeredBy", {
            login: proposal.answered_by,
            state: proposal.state,
            ago: () => <Ago value={proposal.answered_at} never="-" />,
            who: (chunks) => <span className="font-mono text-xs">{chunks}</span>,
          })}
        </p>
        {proposal.state === "deferred" && proposal.deferred_until ? (
          <p className="text-muted-foreground" data-testid="proposal-deferred-until">
            {t.rich("deferredUntil", { at: () => <Ago value={proposal.deferred_until} never="-" /> })}
          </p>
        ) : null}
        {proposal.note ? (
          <p className="rounded-sm bg-surface-sunken px-2.5 py-1.5 text-pretty [overflow-wrap:anywhere]" data-testid="proposal-note">
            {proposal.note}
          </p>
        ) : null}
        {proposal.state === "accepted" ? <p className="text-xs text-fg-subtle">{t("acceptedNext")}</p> : null}
      </div>
    );
  } else {
    body = <p className="text-muted-foreground">{admin ? t("waitingYou") : t("waitingAdmin")}</p>;
  }
  return (
    <HomeCard title={t("answerTitle")} testId="proposal-answer-card">
      <div className="flex flex-col gap-2 px-4 py-3 text-[13px]">
        {body}
        {proposal.inbox_at ? (
          <p className="text-xs text-fg-subtle">{t.rich("inbox", { ago: () => <Ago value={proposal.inbox_at} never="-" /> })}</p>
        ) : null}
      </div>
    </HomeCard>
  );
}

/**
 * The body of a proposal, the same on its page and in the Inbox's sheet: its summary, its evidence and the findings
 * it rests on, its draft plan; and beside them from xl (under them below, two by two from md) the answer and why it
 * has its tier. The state and the answer buttons are in the head, so the page reads the proposal first.
 */
export function ProposalBody({ project, proposal, admin, compact = false }: { project: string; proposal: Proposal; admin: boolean; compact?: boolean }) {
  const t = useTranslations("curator.proposal");
  const repos = useRepos(project);
  return (
    <div className={cn("grid items-start gap-4", !compact && "xl:grid-cols-[minmax(0,1fr)_22rem]")}>
      <div className={cn("flex min-w-0 flex-col gap-4", !compact && "xl:col-start-1 xl:row-start-1")}>
        <HomeCard title={t("summary")} testId="proposal-summary">
          <div className="px-4 py-3">
            {proposal.summary ? <SafeMarkdown testId="proposal-summary-text">{proposal.summary}</SafeMarkdown> : <p className="text-[13px] text-muted-foreground">{t("noSummary")}</p>}
          </div>
        </HomeCard>
        <HomeCard title={t("evidence", { count: proposal.evidence_count })} testId="proposal-evidence">
          <div className="flex flex-col gap-4 px-4 py-3">
            {proposal.evidence.length > 0 || proposal.finding_ids.length === 0 ? (
              <div className="flex flex-col gap-2">
                {proposal.finding_ids.length > 0 ? <h3 className="text-[13px] font-medium text-muted-foreground">{t("ownEvidence")}</h3> : null}
                <EvidenceList project={project} evidence={proposal.evidence as Record<string, unknown>[]} repos={repos} />
              </div>
            ) : null}
            <Findings project={project} ids={proposal.finding_ids} repos={repos} />
          </div>
        </HomeCard>
        <HomeCard title={t("draft")} testId="proposal-draft-card">
          <div className="px-4 py-3">
            <DraftPlan plan={proposal.plan as Record<string, unknown>} />
          </div>
        </HomeCard>
      </div>
      <div className={cn("grid min-w-0 items-start gap-4", !compact && "md:grid-cols-2 xl:col-start-2 xl:row-start-1 xl:grid-cols-1")}>
        <AnswerCard project={project} proposal={proposal} admin={admin} />
        <TierCard proposal={proposal} />
      </div>
    </div>
  );
}

/** The head of a proposal: its title, its state, its tier, kind and lens, the admin's answers, and the run that proposed it. */
function ProposalHead({ project, proposal, onAnswer }: { project: string; proposal: Proposal; onAnswer: ((action: ProposalAction) => void) | null }) {
  const t = useTranslations("curator.proposal");
  return (
    <PageHeader
      title={proposal.title}
      status={<StatusBadge kind="proposal" status={proposal.state} size="lg" />}
      tags={
        <>
          <TierTag tier={proposal.tier} />
          <KindTag kind={proposal.kind} />
          <LensTag lens={proposal.lens} />
        </>
      }
      actions={onAnswer ? <AnswerButtons proposal={proposal} onAnswer={onAnswer} /> : null}
      sub={t.rich("sub", {
        id: proposal.id,
        run: proposal.run_id,
        runLink: (chunks) => (
          <Link href={runHref(project, proposal.run_id)} className={TEXT_LINK} data-testid="proposal-run-link">
            {chunks}
          </Link>
        ),
        ago: () => <Ago value={proposal.created_at} never="-" />,
      })}
      testId="proposal-header"
    />
  );
}

/** A revert the hub proposed itself, once the figures of a merged change got worse: which proposal it undoes. */
function RevertNote({ project, of }: { project: string; of: number }) {
  const t = useTranslations("curator.proposal");
  return (
    <div role="note" className="flex items-start gap-2.5 rounded-md border bg-surface-sunken px-4 py-3 text-[13px] text-muted-foreground" data-testid="proposal-revert-of">
      <Undo2 className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <p className="text-pretty">
        {t.rich("revertOf", {
          id: of,
          link: (chunks) => (
            <Link href={proposalHref(project, of)} className={TEXT_LINK}>
              {chunks}
            </Link>
          ),
        })}
      </p>
    </div>
  );
}

/**
 * One proposal of the Curator: what it would change and why, the evidence behind it (each piece a link to the run,
 * the digest's entry or the line of code), the findings it rests on, its draft plan, its ledger (what happened to it,
 * line by line), and for an admin of the project Accept, Defer and Reject, each confirmed in a dialog with an optional
 * note. A proposal the visitor may not read is not found.
 */
export function ProposalPage({ project, id, initialError }: { project: string; id: number; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("curator.proposal");
  const viewer = useCuratorViewer(project);
  const admin = viewer?.role === "admin";
  const state = useHubQuery(proposalQuery(browserApi, project, id), initialError, { live: true });
  const [action, setAction] = useState<ProposalAction | null>(null);
  if (state.status === "error" && state.error.status === 404) {
    return <NotFoundState title={t("notFoundTitle", { id })} description={t("notFoundDescription")} />;
  }
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(proposal) => (
        <div className="flex flex-col gap-6" data-testid="proposal-page" data-proposal-id={proposal.id} data-state={proposal.state}>
          <ProposalHead project={project} proposal={proposal} onAnswer={admin ? setAction : null} />
          {proposal.state === "dropped" ? (
            <div role="note" className="flex items-start gap-2.5 rounded-md border bg-surface-sunken px-4 py-3 text-[13px] text-muted-foreground">
              <CopyX className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
              <p className="text-pretty">{t("droppedNote")}</p>
            </div>
          ) : !admin ? (
            <div role="note" className="flex items-start gap-2.5 rounded-md border bg-surface-sunken px-4 py-3 text-[13px] text-muted-foreground" data-testid="proposal-read-only">
              <Info className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
              <p className="text-pretty">{t("readOnly")}</p>
            </div>
          ) : null}
          {proposal.revert_of ? <RevertNote project={project} of={proposal.revert_of} /> : null}
          <ProposalBody project={project} proposal={proposal} admin={admin} />
          <LedgerCard project={project} id={proposal.id} />
          {admin ? <AnswerDialog project={project} proposal={proposal} action={action} onClose={() => setAction(null)} /> : null}
        </div>
      )}
    </QueryView>
  );
}
