"use client";

import { GitBranch, GitCommitHorizontal } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";

import { COMMITS_SHOWN, noticeFacts, pluginFacts, shortCommit } from "./model";
import type { Notification } from "./queries";

/** The repo, branch and commits of a push or merge into a default branch. */
export function BranchFacts({ notification }: { notification: Notification }) {
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

/** The plugin that sent a notice and its topic. */
export function PluginNoticeFacts({ notification }: { notification: Notification }) {
  const t = useTranslations("inbox.item");
  const { plugin, topic } = pluginFacts(notification.details);
  if (!plugin && !topic) return null;
  return (
    <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs" data-testid="notice-plugin-facts">
      {plugin ? (
        <>
          <dt className="text-muted-foreground">{t("plugin")}</dt>
          <dd className="font-mono [overflow-wrap:anywhere]" data-testid="notice-plugin">
            {plugin}
          </dd>
        </>
      ) : null}
      {topic ? (
        <>
          <dt className="text-muted-foreground">{t("topic")}</dt>
          <dd className="font-mono [overflow-wrap:anywhere]" data-testid="notice-topic">
            {topic}
          </dd>
        </>
      ) : null}
    </dl>
  );
}
