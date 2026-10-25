"use client";

import { Activity, ExternalLink, GitPullRequestDraft, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { type Chat, chatRunHref, type GroupMessage, noWorkerProject, pullLinks } from "@/lib/chats";

import { BindWorker } from "./bind-worker";

const CHIP =
  "inline-flex h-6 max-w-full items-center gap-1.5 rounded-xs border bg-card px-2 text-xs font-medium text-foreground transition-colors hover:border-brand/40 hover:text-brand max-md:h-8 [&>svg]:size-3.5 [&>svg]:shrink-0";

/**
 * Where a message leads (web/DESIGN.md, Chats): the run an agent's message came from, the run each agent it
 * mentioned took the work in (or why it took none), and the pull requests it links, the agent's draft ones above all.
 * Square chips, since each names a thing.
 */
export function MessageLinks({ message, chat, hideOwnRun = false }: { message: GroupMessage; chat: Pick<Chat, "agents">; hideOwnRun?: boolean }) {
  const t = useTranslations("chats.message");
  const projects = new Map(chat.agents.map((agent) => [agent.handle, agent.project]));
  const author = message.author;
  const own = !hideOwnRun && author.kind === "agent" && author.project && message.run_id ? { project: author.project, run: message.run_id } : null;
  const handed = message.mentions.filter((mention) => mention.handle && (mention.run_id || mention.refusal));
  const pulls = author.kind === "person" ? [] : pullLinks(message.body);
  if (!own && !handed.length && !pulls.length) return null;
  return (
    <ul className="flex flex-wrap items-center gap-1.5" data-testid="message-links">
      {own ? (
        <li>
          <Link href={chatRunHref(own.project, own.run)} className={CHIP} aria-label={t("ownRunLabel", { id: own.run, handle: author.handle ?? "" })} data-testid="message-run-link" data-run-id={own.run}>
            <Activity aria-hidden="true" />
            <span className="tabular-nums">{t("run", { id: own.run })}</span>
          </Link>
        </li>
      ) : null}
      {handed.map((mention) => {
        const project = mention.handle ? projects.get(mention.handle) : undefined;
        if (mention.run_id && project) {
          return (
            <li key={`run-${mention.name}`}>
              <Link
                href={chatRunHref(project, mention.run_id)}
                className={CHIP}
                aria-label={t("handedLabel", { handle: mention.name, id: mention.run_id })}
                data-testid="mention-run-link"
                data-run-id={mention.run_id}
              >
                <Activity aria-hidden="true" />
                <span className="truncate">{t("handed", { handle: mention.name, id: mention.run_id })}</span>
              </Link>
            </li>
          );
        }
        if (mention.run_id) {
          return (
            <li key={`run-${mention.name}`} className={CHIP} data-testid="mention-run" data-run-id={mention.run_id}>
              <Activity aria-hidden="true" />
              <span className="truncate">{t("handed", { handle: mention.name, id: mention.run_id })}</span>
            </li>
          );
        }
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
