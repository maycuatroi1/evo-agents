"use client";

import { Activity, ExternalLink, GitPullRequestDraft, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { type Chat, chatRunHref, type GroupMessage, noWorkerProject, pullLinks } from "@/lib/chats";

import { BindWorker } from "./bind-worker";

const CHIP =
  "inline-flex h-6 max-w-full items-center gap-1.5 rounded-xs border bg-card px-2 text-xs font-medium text-foreground transition-colors hover:border-brand/40 hover:text-brand max-md:h-8 [&>svg]:size-3.5 [&>svg]:shrink-0";

/**
 * Where a message leads (web/DESIGN.md, Chats): why a mention took no work, and the pull requests it links, the agent's
 * draft ones above all. Square chips, since each names a thing. The runs it came from or handed work to are not here:
 * `MessageRuns` puts them among the message's actions, shown while the pointer is over it.
 */
export function MessageLinks({ message }: { message: GroupMessage }) {
  const t = useTranslations("chats.message");
  const refused = message.mentions.filter((mention) => mention.handle && !mention.run_id && mention.refusal);
  const pulls = message.author.kind === "person" ? [] : pullLinks(message.body);
  if (!refused.length && !pulls.length) return null;
  return (
    <ul className="flex flex-wrap items-center gap-1.5" data-testid="message-links">
      {refused.map((mention) => {
        const unbound = noWorkerProject(mention.refusal);
        return (
          <li key={`refused-${mention.name}`} className="flex min-w-0 flex-wrap items-start gap-1.5 text-xs text-attention" data-testid="mention-refused" data-handle={mention.name}>
            <TriangleAlert className="mt-px size-3.5 shrink-0" aria-hidden="true" />
            <span className="min-w-0 [overflow-wrap:anywhere]">
              <span className="font-medium">{t("refused", { handle: mention.name })}</span> {mention.refusal}
            </span>
            {unbound ? <BindWorker project={unbound} /> : null}
          </li>
        );
      })}
      {pulls.map((pull) => (
        <li key={pull.url}>
          <a href={pull.url} target="_blank" rel="noopener noreferrer" className={CHIP} data-testid="message-pull-link" data-pull={pull.number}>
            <GitPullRequestDraft aria-hidden="true" />
            <span className="truncate">{t("pull", { number: pull.number, repo: pull.repo })}</span>
            <ExternalLink aria-hidden="true" />
            <span className="sr-only">{t("newTab")}</span>
          </a>
        </li>
      ))}
    </ul>
  );
}

/**
 * The runs of a message, among its actions (shown while the pointer is over it, from 768 px): the run an agent's
 * message came from and the run each agent it mentioned took the work in, an icon button each whose name and tooltip
 * say which run. A run whose agent's project the chat does not know shows nothing, having nowhere to lead.
 */
export function MessageRuns({ message, chat }: { message: GroupMessage; chat: Pick<Chat, "agents"> }) {
  const t = useTranslations("chats.message");
  const projects = new Map(chat.agents.map((agent) => [agent.handle, agent.project]));
  const author = message.author;
  const runs: { key: string; href: string; label: string; testId: string; run: number }[] = [];
  if (author.kind === "agent" && author.project && message.run_id) {
    runs.push({
      key: "own",
      href: chatRunHref(author.project, message.run_id),
      label: t("ownRunLabel", { id: message.run_id, handle: author.handle ?? "" }),
      testId: "message-run-link",
      run: message.run_id,
    });
  }
  for (const mention of message.mentions) {
    const project = mention.handle ? projects.get(mention.handle) : undefined;
    if (!mention.run_id || !project) continue;
    runs.push({
      key: `run-${mention.name}`,
      href: chatRunHref(project, mention.run_id),
      label: t("handedLabel", { handle: mention.name, id: mention.run_id }),
      testId: "mention-run-link",
      run: mention.run_id,
    });
  }
  return runs.map((run) => (
    <Button key={run.key} asChild variant="ghost" size="icon-sm">
      <Link href={run.href} aria-label={run.label} title={run.label} data-testid={run.testId} data-run-id={run.run}>
        <Activity aria-hidden="true" />
      </Link>
    </Button>
  ));
}
