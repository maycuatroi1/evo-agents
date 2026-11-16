"use client";

import { ExternalLink } from "lucide-react";
import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";

import { safeUrl } from "./ledger-model";
import { TEXT_LINK } from "./parts";
import type { Change } from "./queries";

type ChangeRepo = NonNullable<Change["repos"]>[number];

function RepoPull({ item }: { item: ChangeRepo }) {
  const t = useTranslations("curator.proposal.change");
  const url = safeUrl(item.pr_url);
  if (!url || item.pr_number === null) return null;
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={t("pullLabel", { number: item.pr_number })}
      className={cn(TEXT_LINK, "inline-flex w-fit items-center gap-1 font-mono text-[13px] tabular-nums")}
      data-testid="proposal-change-repo-pr"
    >
      #{item.pr_number}
      <ExternalLink className="size-3.5 shrink-0" aria-hidden="true" />
    </a>
  );
}

/**
 * The repos of a change that works in several (docs/curator.md, A change in several repos), in plan order: an owner of
 * a seam before the repos that consume it, each with the repos merged before it, its own state and pull request. A
 * change in one repo shows nothing here.
 */
export function ChangeRepos({ change }: { change: Change }) {
  const t = useTranslations("curator.proposal.change");
  const repos = change.repos ?? [];
  if (repos.length === 0) return null;
  return (
    <ol className="mx-4 mb-4 flex flex-col divide-y rounded-md border text-[13px]" aria-label={t("repos")} data-testid="proposal-change-repos">
      {repos.map((item) => (
        <li key={item.repo} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2" data-testid="proposal-change-repo-row" data-state={item.state}>
          <span className="tabular-nums text-muted-foreground">{item.position}.</span>
          <span className="font-mono text-xs text-foreground">{item.repo}</span>
          <span className="text-muted-foreground">{item.state}</span>
          <RepoPull item={item} />
          {item.depends_on.length > 0 ? (
            <span className="basis-full text-xs text-fg-subtle" data-testid="proposal-change-repo-after">
              {t("repoAfter", { repos: item.depends_on.join(", ") })}
            </span>
          ) : null}
        </li>
      ))}
    </ol>
  );
}
