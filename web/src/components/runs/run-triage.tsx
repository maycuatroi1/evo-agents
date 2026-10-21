"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Check, ChevronRight, CircleX, MessagesSquare, X } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type ReactNode, useId } from "react";

import { Tag } from "@/components/data/identifier";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import type { Run } from "./queries";
import {
  type AgentCandidate,
  causeKey,
  type OtherProject,
  type RerunCandidates,
  runCandidatesQuery,
  TRIAGE_KINDS,
  triageHref,
  type WorkerCandidate,
} from "./triage-queries";

/**
 * Over a failed run's trace: why it failed (the structured cause, and whether the hub only guessed it from the error),
 * the thread the hub opened for it in the triage chat, and who could run it again (docs/workers.md, Failure triage).
 * The run's owner reads the candidates (the hub answers nobody else), so another member sees the cause alone.
 */
export function RunTriage({ run, owner }: { run: Run; owner: boolean }) {
  const t = useTranslations("runs.detail.triage");
  const ids = useId();
  const failed = run.state === "failed" && TRIAGE_KINDS.includes(run.kind);
  const found = useQuery({ ...runCandidatesQuery(browserApi, run.project, run.id), enabled: failed && owner });
  if (!failed) return null;
  const cause = found.data?.failure_cause ?? run.failure_cause ?? null;
  if (!cause && !owner) return null;
  const data = found.data;
  const thread = data?.triage ?? null;

  return (
    <section
      aria-labelledby={`${ids}-title`}
      className="flex min-w-0 flex-col gap-3 rounded-md border border-danger/20 bg-card p-4"
      data-testid="run-triage"
      data-cause={cause ?? undefined}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <h2 id={`${ids}-title`} className="flex min-w-0 items-center gap-2 text-[15px] leading-[22px] font-semibold">
          <CircleX className="size-4 shrink-0 text-danger" aria-hidden="true" />
          {t("title")}
        </h2>
        {thread ? (
          <Button asChild size="sm" variant="outline" className="ml-auto max-md:h-11">
            <Link href={triageHref(thread.link)} data-testid="run-triage-thread">
              <MessagesSquare aria-hidden="true" />
              {t("openThread")}
              <ArrowRight aria-hidden="true" />
            </Link>
          </Button>
        ) : null}
      </div>
      <Cause cause={cause} guessed={data?.cause_inferred ?? false} />
      {owner ? <Candidates state={found} thread={thread !== null} /> : null}
    </section>
  );
}

function Cause({ cause, guessed }: { cause: string | null; guessed: boolean }) {
  const t = useTranslations("runs.detail.triage");
  if (!cause) return <p className="text-sm text-muted-foreground">{t("noCause")}</p>;
  const key = causeKey(cause);
  return (
    <div className="flex flex-col gap-1.5">
      <p className="flex flex-wrap items-center gap-2 text-sm">
        <Tag data-testid="run-triage-cause" className="font-mono">
          {cause}
        </Tag>
        <span className="font-medium text-foreground">{t(`cause.${key}.label`)}</span>
        {guessed ? (
          <Tag className="bg-attention-soft text-attention" data-testid="run-triage-guessed">
            {t("guessed")}
          </Tag>
        ) : null}
      </p>
      <p className="text-[13px] leading-[18px] text-pretty text-muted-foreground">
        {t(`cause.${key}.hint`)}
        {guessed ? ` ${t("guessedNote")}` : ""}
      </p>
    </div>
  );
}

type Candidates = ReturnType<typeof useQuery<RerunCandidates>>;

function Candidates({ state, thread }: { state: Candidates; thread: boolean }) {
  const t = useTranslations("runs.detail.triage");
  if (state.isPending) return <Skeleton className="h-9 w-full" data-testid="run-triage-loading" />;
  if (state.isError || !state.data) {
    return (
      <p className="text-[13px] leading-[18px] text-muted-foreground" data-testid="run-triage-error">
        {t("candidatesFailed")}
      </p>
    );
  }
  const { workers, agents, other_projects: others = [] } = state.data;
  const fitWorkers = workers.filter((worker) => worker.fit).length;
  const fitAgents = agents.filter((agent) => agent.fit).length;
  return (
    <div className="flex flex-col gap-2.5">
      {thread ? null : (
        <p className="text-[13px] leading-[18px] text-pretty text-muted-foreground" data-testid="run-triage-no-thread">
          {t("noThread")}
        </p>
      )}
      <details className="group/cand rounded-md border bg-background" data-testid="run-triage-candidates">
        <summary className="flex min-h-10 cursor-pointer list-none items-center gap-2 rounded-md px-3 py-2 text-sm hover:bg-muted/50 max-md:min-h-11 [&::-webkit-details-marker]:hidden">
          <ChevronRight className="size-4 shrink-0 text-muted-foreground transition-transform group-open/cand:rotate-90" aria-hidden="true" />
          <span className="font-medium">{t("candidates")}</span>
          <span className="text-muted-foreground" data-testid="run-triage-summary">
            {t("candidatesSummary", { fitWorkers, workers: workers.length, fitAgents, agents: agents.length })}
          </span>
        </summary>
        <div className="flex flex-col gap-4 border-t p-3">
          <CandidateGroup title={t("workers")} empty={t("noWorkers")} count={workers.length}>
            {workers.map((worker) => (
              <WorkerRow key={worker.worker_id} worker={worker} />
            ))}
          </CandidateGroup>
          <CandidateGroup title={t("agents")} empty={t("noAgents")} count={agents.length}>
            {agents.map((agent) => (
              <AgentRow key={`${agent.project}/${agent.handle}`} agent={agent} />
            ))}
          </CandidateGroup>
          {others.length > 0 ? (
            <CandidateGroup title={t("otherProjects")} empty="" count={others.length}>
              {others.map((other) => (
                <OtherRow key={other.project} other={other} />
              ))}
            </CandidateGroup>
          ) : null}
        </div>
      </details>
    </div>
  );
}

function CandidateGroup({ title, empty, count, children }: { title: string; empty: string; count: number; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <h3 className="text-xs font-medium tracking-wide text-fg-subtle uppercase">{title}</h3>
      {count === 0 ? <p className="text-[13px] text-muted-foreground">{empty}</p> : <ul className="flex flex-col divide-y rounded-md border">{children}</ul>}
    </div>
  );
}

/** The verdict as a word and an icon, never by colour alone. */
function Fit({ fit, rank }: { fit: boolean; rank: number | null }) {
  const t = useTranslations("runs.detail.triage");
  return (
    <Tag className={cn(fit ? "bg-success-soft text-success" : "bg-danger-soft text-danger")} data-fit={fit} data-testid="run-triage-fit">
      {fit ? <Check aria-hidden="true" /> : <X aria-hidden="true" />}
      {fit ? (rank ? t("fitRank", { rank }) : t("fit")) : t("noFit")}
    </Tag>
  );
}

function Reasons({ reasons }: { reasons: string[] }) {
  if (reasons.length === 0) return null;
  return (
    <ul className="flex flex-col gap-0.5 text-[13px] leading-[18px] text-pretty text-muted-foreground">
      {reasons.map((reason) => (
        <li key={reason}>{reason}</li>
      ))}
    </ul>
  );
}

function Row({ name, tags, fit, rank, reasons, testId }: { name: ReactNode; tags: ReactNode; fit: boolean; rank: number | null; reasons: string[]; testId: string }) {
  return (
    <li className="flex min-w-0 flex-col gap-1 px-3 py-2.5" data-testid={testId}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="min-w-0 truncate text-sm font-medium">{name}</span>
        {tags}
        <span className="ml-auto">
          <Fit fit={fit} rank={rank} />
        </span>
      </div>
      <Reasons reasons={reasons} />
    </li>
  );
}

function WorkerRow({ worker }: { worker: WorkerCandidate }) {
  const t = useTranslations("runs.detail.triage");
  return (
    <Row
      testId="run-triage-worker"
      name={worker.worker}
      fit={worker.fit}
      rank={worker.rank}
      reasons={worker.reasons}
      tags={
        <>
          {worker.is_run_worker ? <Tag>{t("runWorker")}</Tag> : null}
          {worker.online ? null : <Tag>{t("offline")}</Tag>}
        </>
      }
    />
  );
}

function AgentRow({ agent }: { agent: AgentCandidate }) {
  const t = useTranslations("runs.detail.triage");
  return (
    <Row
      testId="run-triage-agent"
      name={
        <>
          {agent.name} <span className="font-mono text-xs font-normal text-muted-foreground">@{agent.handle}</span>
        </>
      }
      fit={agent.fit}
      rank={agent.rank}
      reasons={agent.reasons}
      tags={
        <>
          {agent.is_default ? <Tag>{t("defaultAgent")}</Tag> : null}
          <Tag className="font-mono">{agent.project}</Tag>
        </>
      }
    />
  );
}

function OtherRow({ other }: { other: OtherProject }) {
  const t = useTranslations("runs.detail.triage");
  return (
    <Row
      testId="run-triage-other"
      name={other.project}
      fit={other.fit}
      rank={null}
      reasons={[t("otherNamed", { named: other.named }), ...other.reasons]}
      tags={<Tag>{other.role ?? t("noGrant")}</Tag>}
    />
  );
}
