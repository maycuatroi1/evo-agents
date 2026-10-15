"use client";

import { CircleAlert, ExternalLink, Info } from "lucide-react";
import type { Route } from "next";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Identifier } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { projectHref } from "@/components/shell/nav";
import { cn } from "@/lib/utils";

import { branchUrl } from "./forge";
import { safeUrl } from "./ledger-model";
import type { RepoInfo } from "./model";
import { TEXT_LINK } from "./parts";
import type { Change } from "./queries";

/**
 * What accepting a proposal of tier 0 to 2 made, as the hub holds it (`Proposal.change`, docs/curator.md): the Curator's
 * plan, as a link to its page; its repo and branch, the branch a link to the forge when the repo's origin gives one; its
 * pull request once the hub opened it; when it last moved; and the reason the hub kept while the change waits or stays
 * open. The page puts it in a card whose head carries the change's state.
 */

/**
 * A fact of the change: its term over its value while the card is narrow (the page's side column, two cards side by
 * side from md, a phone), side by side once the card is 24rem wide (the Inbox's sheet), so a plan id or a branch keeps
 * the width it needs.
 */
function Fact({ label, children, testId }: { label: ReactNode; children: ReactNode; testId: string }) {
  return (
    <>
      <dt className="text-xs leading-5 text-muted-foreground @sm:text-[13px]">{label}</dt>
      <dd className="-mt-2 min-w-0 text-foreground [overflow-wrap:anywhere] @sm:mt-0" data-testid={testId}>
        {children}
      </dd>
    </>
  );
}

/** A plan's page, as plans/links.ts makes it (a folder this one does not import). */
function planPage(project: string, plan: string): Route {
  return projectHref(project, `plans/${encodeURIComponent(plan)}`);
}

/** Why the change stands where it is: in `attention` when it is left open for a person, on `surface-sunken` otherwise. */
function ReasonNote({ change }: { change: Change }) {
  const t = useTranslations("curator.proposal.change");
  if (!change.reason) return null;
  const warning = change.state === "open";
  const Icon = warning ? CircleAlert : Info;
  return (
    <div
      className={cn(
        "mx-4 mb-4 flex items-start gap-2.5 rounded-md px-3 py-2.5 text-[13px] text-foreground",
        warning ? "border border-attention/30 bg-attention-soft" : "bg-surface-sunken",
      )}
      data-testid="proposal-change-reason"
      data-tone={warning ? "attention" : "info"}
    >
      <Icon className={cn("mt-0.5 size-4 shrink-0", warning ? "text-attention" : "text-muted-foreground")} aria-hidden="true" />
      <div className="flex min-w-0 flex-col gap-1">
        <h3 className="font-medium">{t("reasonTitle", { state: change.state })}</h3>
        <p className="text-pretty [overflow-wrap:anywhere]" data-testid="proposal-change-reason-text">
          {change.reason}
        </p>
      </div>
    </div>
  );
}

function PullLink({ change }: { change: Change }) {
  const t = useTranslations("curator.proposal.change");
  const url = safeUrl(change.pr_url);
  if (!url) return null;
  const numbered = change.pr_number !== null;
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={numbered ? t("pullLabel", { number: change.pr_number ?? 0 }) : undefined}
      className={cn(TEXT_LINK, "inline-flex w-fit items-center gap-1", numbered && "font-mono text-[13px] tabular-nums")}
      data-testid="proposal-change-pr"
    >
      {numbered ? `#${change.pr_number}` : t("pullPlain")}
      <ExternalLink className="size-3.5 shrink-0" aria-hidden="true" />
      {numbered ? null : <span className="sr-only">{t("newTab")}</span>}
    </a>
  );
}

export function ChangeFacts({ project, change, repos }: { project: string; change: Change; repos: readonly RepoInfo[] }) {
  const t = useTranslations("curator.proposal.change");
  const origin = repos.find((repo) => repo.name === change.repo)?.origin ?? null;
  return (
    <div className="flex min-w-0 flex-col" data-testid="proposal-change-body" data-state={change.state}>
      <dl className="grid grid-cols-1 gap-x-4 gap-y-2.5 px-4 py-3 text-sm @sm:grid-cols-[minmax(6rem,max-content)_minmax(0,1fr)]" data-testid="proposal-change-facts">
        <Fact label={t("plan")} testId="proposal-change-plan">
          {change.plan_id ? (
            <Identifier value={change.plan_id} href={planPage(project, change.plan_id)} title={change.plan_id} testId="proposal-change-plan-link" />
          ) : (
            <span className="text-[13px] text-muted-foreground">{t("noPlan")}</span>
          )}
        </Fact>
        {change.repo ? (
          <Fact label={t("repo")} testId="proposal-change-repo">
            <span className="font-mono text-xs">{change.repo}</span>
          </Fact>
        ) : null}
        {change.branch ? (
          <Fact label={t("branch")} testId="proposal-change-branch">
            <Identifier
              value={change.branch}
              title={change.branch}
              externalHref={branchUrl(origin, change.branch, change.forge)}
              linkLabel={t("openBranch", { branch: change.branch })}
              copy
              copyLabel={t("copyBranch", { branch: change.branch })}
              testId="proposal-change-branch-link"
            />
          </Fact>
        ) : null}
        {safeUrl(change.pr_url) ? (
          <Fact label={t("pull")} testId="proposal-change-pull">
            <PullLink change={change} />
          </Fact>
        ) : null}
        <Fact label={t("moved")} testId="proposal-change-moved">
          <span className="text-[13px]">
            <When value={change.updated_at} />
          </span>
        </Fact>
      </dl>
      <ReasonNote change={change} />
    </div>
  );
}
