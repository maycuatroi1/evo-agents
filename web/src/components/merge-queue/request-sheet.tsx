"use client";

import { ArrowRight, CircleAlert, CircleCheck, CircleX, ExternalLink, GitBranch, X } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type ReactNode, useRef } from "react";

import { Identifier, RunRef } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { Banner, type BannerTone } from "@/components/feedback/banner";
import { projectHref } from "@/components/shell/nav";
import { Button } from "@/components/ui/button";
import { Sheet, SheetClose, SheetContent, SheetTitle } from "@/components/ui/sheet";
import type { QueuedMergeRequest } from "@/lib/merge-queue";
import { cn } from "@/lib/utils";

import { CheckBadge, MergeStateBadge, SourceTag } from "./badges";
import { TEXT_LINK } from "./chrome";
import { Ledger } from "./ledger";
import { forgeUrl, groupOf, isCheckResult, judgeVerdicts, requestName, riskOf, shortSha } from "./model";
import { RequestActions } from "./request-actions";

function Fact({ label, children, testId }: { label: string; children: ReactNode; testId?: string }) {
  return (
    <>
      <dt className="text-xs leading-5 text-muted-foreground">{label}</dt>
      <dd className="-mt-2 min-w-0 text-[13px] [overflow-wrap:anywhere] sm:mt-0" data-testid={testId}>
        {children}
      </dd>
    </>
  );
}

function Sha({ sha }: { sha: string | null }) {
  return sha ? (
    <Identifier value={sha} title={sha} copy>
      {shortSha(sha)}
    </Identifier>
  ) : (
    <span className="text-fg-subtle">-</span>
  );
}

function Section({ title, children, testId }: { title: string; children: ReactNode; testId?: string }) {
  return (
    <section className="flex flex-col gap-2" data-testid={testId}>
      <h3 className="text-[13px] font-semibold">{title}</h3>
      {children}
    </section>
  );
}

/** Why a merge request waits, is blocked or is undone, in the tone of where it stands. */
function reasonTone(state: string): BannerTone {
  if (state === "blocked" || state === "paused") return "attention";
  if (state === "reverting" || state === "rolling_back") return "danger";
  return "neutral";
}

/** The Judges' verdicts, each dimension with its reason, and the risk the gate found. */
function Verdicts({ request }: { request: QueuedMergeRequest }) {
  const t = useTranslations("mergeQueue.request");
  const judges = judgeVerdicts(request.verdict);
  const risk = riskOf(request.risk);
  if (judges.length === 0 && !risk) return null;
  return (
    <Section title={t("judges")} testId="merge-verdicts">
      {risk ? (
        <div className="flex flex-col gap-1 text-[13px]" data-testid="merge-risk" data-high={risk.high ? "true" : "false"}>
          <p className="font-medium text-foreground">{risk.high ? t("riskHigh") : t("riskNormal")}</p>
          {risk.reasons.length > 0 ? (
            <ul className="list-disc pl-5 text-xs text-muted-foreground">
              {risk.reasons.map((reason) => (
                <li key={reason} className="[overflow-wrap:anywhere]">
                  {reason}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
      {judges.map((judge) => (
        <div key={judge.judge} className="flex flex-col gap-1.5 rounded-sm border px-3 py-2.5" data-testid="merge-judge" data-judge={judge.judge}>
          <p className="flex flex-wrap items-center gap-2 text-[13px] font-medium">
            {judge.passed === null ? null : judge.passed ? (
              <CircleCheck className="size-4 text-success" aria-hidden="true" />
            ) : (
              <CircleX className="size-4 text-danger" aria-hidden="true" />
            )}
            {t(judge.judge === "first" ? "firstJudge" : "secondJudge")}
            <span className="text-muted-foreground">{judge.passed === null ? t("noVerdict") : judge.passed ? t("judgePassed") : t("judgeFailed")}</span>
            {judge.model ? <span className="font-mono text-xs text-fg-subtle">{judge.model}</span> : null}
          </p>
          {judge.failures.length > 0 ? (
            <ul className="list-disc pl-5 text-xs text-danger">
              {judge.failures.map((failure) => (
                <li key={failure} className="[overflow-wrap:anywhere]">
                  {failure}
                </li>
              ))}
            </ul>
          ) : null}
          {judge.dimensions.length > 0 ? (
            <ul className="flex flex-col gap-1">
              {judge.dimensions.map((dimension) => (
                <li key={dimension.name} className="flex min-w-0 gap-2 text-xs" data-dimension={dimension.name}>
                  <span className={cn("shrink-0 font-medium", dimension.passed === false ? "text-danger" : "text-foreground")}>
                    {dimension.name}: {dimension.passed === null ? "?" : dimension.passed ? t("pass") : t("fail")}
                  </span>
                  {dimension.reason ? <span className="min-w-0 text-muted-foreground [overflow-wrap:anywhere]">{dimension.reason}</span> : null}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ))}
    </Section>
  );
}

/** Everything the hub keeps of a merge request of the queue, and what an admin may do to it now. */
export function RequestDetail({ project, request, admin, hrefOf }: { project: string; request: QueuedMergeRequest; admin: boolean; hrefOf: (id: number) => Route }) {
  const t = useTranslations("mergeQueue.request");
  const url = forgeUrl(request.web_url);
  const name = requestName(request);
  return (
    <div className="flex flex-col gap-5 p-4" data-testid="merge-request" data-id={request.id} data-state={request.state}>
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <MergeStateBadge state={request.state} />
          <SourceTag source={request.source} />
          <span className="font-mono text-xs text-muted-foreground">{name}</span>
        </div>
        <h2 className="text-[15px] leading-[22px] font-semibold text-pretty [overflow-wrap:anywhere]" data-testid="merge-request-title">
          {request.title ?? name}
        </h2>
        {url ? (
          <a href={url} target="_blank" rel="noopener noreferrer" className={cn(TEXT_LINK, "w-fit text-[13px]")} data-testid="merge-request-forge">
            {t("openForge", { name })}
            <ExternalLink className="ml-1 inline size-3.5 align-[-2px]" aria-hidden="true" />
            <span className="sr-only">{t("opensNewTab")}</span>
          </a>
        ) : null}
        <RequestActions project={project} request={request} admin={admin} />
      </div>
      {request.reason ? (
        <Banner icon={CircleAlert} tone={reasonTone(request.state)} title={groupOf(request.state) === "blocked" ? t("blockedWhy") : t("why")} testId="merge-request-reason">
          <p className="[overflow-wrap:anywhere]">{request.reason}</p>
        </Banner>
      ) : null}
      <dl className="grid grid-cols-1 gap-x-4 gap-y-2.5 sm:grid-cols-[minmax(7rem,max-content)_minmax(0,1fr)]" data-testid="merge-request-facts">
        <Fact label={t("repo")}>
          <span className="font-mono">{request.repo}</span>
        </Fact>
        <Fact label={t("branches")}>
          <span className="inline-flex min-w-0 flex-wrap items-center gap-1 font-mono text-xs">
            <GitBranch className="size-3.5 text-muted-foreground" aria-hidden="true" />
            {request.source_branch}
            <ArrowRight className="size-3.5 text-muted-foreground" aria-hidden="true" />
            <span className="sr-only">{t("into")}</span>
            {request.target_branch}
          </span>
        </Fact>
        {request.run_id !== null ? (
          <Fact label={t("run")}>
            <RunRef id={request.run_id} href={projectHref(project, `runs/${request.run_id}`)} label={t("openRun", { id: request.run_id })} />
          </Fact>
        ) : null}
        {request.plan_id ? (
          <Fact label={t("plan")}>
            <Link href={projectHref(project, `plans/${encodeURIComponent(request.plan_id)}`)} className={cn(TEXT_LINK, "font-mono text-xs")}>
              {request.plan_id}
            </Link>
            {request.steps.length > 0 ? <span className="text-muted-foreground"> {t("steps", { steps: request.steps.join(", ") })}</span> : null}
          </Fact>
        ) : null}
        {request.profile || request.model ? (
          <Fact label={t("agent")}>
            <span className="font-mono text-xs">{[request.profile, request.model].filter(Boolean).join(" / ")}</span>
          </Fact>
        ) : null}
        <Fact label={t("head")} testId="merge-request-head">
          <Sha sha={request.head_sha} />
        </Fact>
        <Fact label={t("rebasedOnto")} testId="merge-request-before">
          <Sha sha={request.before_sha ?? null} />
          {request.rebases ? <span className="ml-2 text-xs text-muted-foreground">{t("rebases", { count: request.rebases })}</span> : null}
        </Fact>
        <Fact label={t("mergedAs")} testId="merge-request-after">
          <Sha sha={request.after_sha ?? null} />
        </Fact>
        <Fact label={t("queuedAt")}>
          <When value={request.queued_at} never="-" short />
        </Fact>
        <Fact label={t("mergedAt")}>
          <When value={request.merged_at} never="-" short />
        </Fact>
        {isCheckResult(request.uat_result) || isCheckResult(request.prod_result) ? (
          <Fact label={t("checks")}>
            <span className="inline-flex flex-wrap gap-1.5">
              {isCheckResult(request.uat_result) ? <CheckBadge check="uat" result={request.uat_result} /> : null}
              {isCheckResult(request.prod_result) ? <CheckBadge check="prod" result={request.prod_result} /> : null}
            </span>
          </Fact>
        ) : null}
        {request.reverts_id ? (
          <Fact label={t("reverts")}>
            <Link href={hrefOf(request.reverts_id)} className={TEXT_LINK} data-testid="merge-request-reverts">
              {t("revertsOf", { id: request.reverts_id })}
            </Link>
          </Fact>
        ) : null}
      </dl>
      <Verdicts request={request} />
      <Section title={t("ledger", { count: request.events.length })}>
        <Ledger project={project} events={request.events} />
      </Section>
    </div>
  );
}

/**
 * The merge request `?mr=ID` names, over the queue's list: from the right, the whole width of a phone and 576 px from
 * the sm breakpoint, its bar naming it beside a close button. Closing gives focus back to what opened it.
 */
export function RequestSheet({
  project,
  request,
  missing,
  admin,
  onClose,
  hrefOf,
}: {
  project: string;
  request: QueuedMergeRequest | null;
  /** The URL names a merge request the list does not hold. */
  missing: number | null;
  admin: boolean;
  onClose: () => void;
  hrefOf: (id: number) => Route;
}) {
  const t = useTranslations("mergeQueue.request");
  const opener = useRef<HTMLElement | null>(null);
  const open = request !== null || missing !== null;
  return (
    <Sheet open={open} onOpenChange={(next) => (next ? undefined : onClose())}>
      <SheetContent
        side="right"
        showCloseButton={false}
        aria-describedby={undefined}
        className="gap-0 p-0 data-[side=right]:w-full data-[side=right]:sm:max-w-xl"
        onOpenAutoFocus={() => {
          const active = document.activeElement;
          opener.current = active instanceof HTMLElement && active !== document.body ? active : null;
        }}
        onCloseAutoFocus={(event) => {
          if (opener.current?.isConnected) {
            event.preventDefault();
            opener.current.focus();
          }
          opener.current = null;
        }}
        data-testid="merge-request-sheet"
      >
        <div className="flex h-13 shrink-0 items-center gap-2 border-b px-4">
          <SheetTitle asChild>
            <p className="min-w-0 truncate text-[15px] leading-[22px] font-semibold">
              {request ? t("sheetTitle", { name: requestName(request), repo: request.repo }) : t("missingTitle", { id: missing ?? 0 })}
            </p>
          </SheetTitle>
          <SheetClose asChild>
            <Button type="button" variant="ghost" size="icon" className="ml-auto" aria-label={t("close")} data-testid="merge-request-close">
              <X aria-hidden="true" />
            </Button>
          </SheetClose>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
          {request ? (
            <RequestDetail project={project} request={request} admin={admin} hrefOf={hrefOf} />
          ) : (
            <p className="p-4 text-[13px] text-muted-foreground" data-testid="merge-request-missing">
              {t("missing")}
            </p>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
