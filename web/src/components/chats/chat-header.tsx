"use client";

import { ArrowLeft, Bot, FolderKanban, Info, Users } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { AgentAvatar } from "@/components/data/agent-avatar";
import { NAME_LINK, Tag } from "@/components/data/identifier";
import { VisibilityTag } from "@/components/data/visibility";
import { InlineError, type WriteFailure } from "@/components/feedback/notice";
import { PageHeader } from "@/components/shell/page-header";
import { Button } from "@/components/ui/button";
import { agentHref } from "@/lib/agent-queries";
import { agentAuthor, type Chat, type ChatAgent, chatsHref } from "@/lib/chats";

import { ChatAvatar } from "./chat-avatar";
import { ChatImageControl } from "./chat-image-control";

/** The levels of a chat's label, each once, as the hub sends them: every member clears them all. */
function levelsOf(chat: Pick<Chat, "label">): string[] {
  const levels = chat.label.map((label) => (typeof label.level === "string" ? label.level : null)).filter((level): level is string => level !== null);
  return [...new Set(levels)];
}

function BackLink() {
  const t = useTranslations("chats.header");
  return (
    <Button asChild variant="ghost" size="icon-sm" className="-ml-1 lg:hidden">
      <Link href={chatsHref()} aria-label={t("back")} data-testid="chat-back">
        <ArrowLeft aria-hidden="true" />
      </Link>
    </Button>
  );
}

/**
 * The head of a chat (web/DESIGN.md, Chats), its title the page's h1 beside its face, the visibility its agents'
 * projects give it. A group chat's face is a button that changes or removes its image (`ChatImageControl`), and its
 * head says who is in it, with Members, which opens the sheet that adds and takes them out. A direct chat's head is its
 * agent's: its avatar and name, its @handle linking to the agent's page, its project, and Details, which opens the same
 * sheet without adding or taking out anyone. Under 1024 px a back link to the list stands before the face.
 */
export function ChatHeader({ chat, me, onMembers }: { chat: Chat; me: string | null; onMembers: () => void }) {
  const [problem, setProblem] = useState<WriteFailure | null>(null);
  const agent = chat.kind === "direct" ? (chat.agents[0] ?? null) : null;
  return (
    <div className="flex flex-col gap-3 border-b px-3 py-3 sm:px-4" data-testid="chat-head" data-kind={chat.kind}>
      <div className="flex items-center gap-2">
        <BackLink />
        {agent ? (
          <DirectHead chat={chat} agent={agent} onDetails={onMembers} />
        ) : (
          <GroupHead chat={chat} me={me} onMembers={onMembers} image={<ChatImageControl chat={chat} me={me} onProblem={setProblem} />} />
        )}
      </div>
      {problem ? (
        <div data-testid="chat-image-problem">
          <InlineError text={problem.text} detail={problem.detail} requestId={problem.requestId} />
        </div>
      ) : null}
    </div>
  );
}

function GroupHead({ chat, me, onMembers, image }: { chat: Chat; me: string | null; onMembers: () => void; image: ReactNode }) {
  const t = useTranslations("chats.header");
  const format = useFormatter();
  const agents = chat.agents.filter((agent) => !agent.deleted);
  const names = [...agents.map((agent) => `@${agent.handle}`), ...chat.people.filter((person) => person.login.toLowerCase() !== me?.toLowerCase()).map((person) => person.login)];
  const shown = names.slice(0, 4);
  const who = names.length > shown.length ? t("andMore", { names: shown.join(", "), count: names.length - shown.length }) : names.length ? format.list(shown, { type: "conjunction" }) : t("onlyYou");
  return (
    <PageHeader
      className="min-w-0 flex-1"
      leading={image}
      title={<span id="chat-title" data-testid="chat-title">{chat.title}</span>}
      tags={levelsOf(chat).map((level) => (
        <VisibilityTag key={level} level={level} testId="chat-visibility" />
      ))}
      actions={
        <>
          {agents.length ? (
            <span className="hidden -space-x-1.5 sm:flex" aria-hidden="true">
              {agents.slice(0, 4).map((agent) => (
                <AgentAvatar key={agent.handle} agent={agent} size="sm" className="ring-2 ring-card" />
              ))}
            </span>
          ) : null}
          <Button type="button" variant="outline" size="sm" onClick={onMembers} data-testid="chat-members-open">
            <Users aria-hidden="true" />
            <span className="tabular-nums">{t("members", { count: chat.people.length + chat.agents.length })}</span>
          </Button>
        </>
      }
      sub={<span data-testid="chat-who">{who}</span>}
      testId="chat-header"
    />
  );
}

function DirectHead({ chat, agent, onDetails }: { chat: Chat; agent: ChatAgent; onDetails: () => void }) {
  const t = useTranslations("chats.header");
  return (
    <PageHeader
      className="min-w-0 flex-1"
      leading={<ChatAvatar face={{ kind: "agent", agent: agentAuthor(agent) }} size="lg" />}
      title={<span id="chat-title" data-testid="chat-title">{agent.name}</span>}
      tags={
        <>
          <Tag data-testid="chat-direct-tag">
            <Bot aria-hidden="true" />
            {t("direct")}
          </Tag>
          {agent.deleted ? <Tag className="text-danger">{t("deleted")}</Tag> : null}
          {levelsOf(chat).map((level) => (
            <VisibilityTag key={level} level={level} testId="chat-visibility" />
          ))}
        </>
      }
      actions={
        <Button type="button" variant="outline" size="sm" onClick={onDetails} data-testid="chat-details-open">
          <Info aria-hidden="true" />
          {t("details")}
        </Button>
      }
      sub={
        <span className="inline-flex max-w-full flex-wrap items-center gap-x-3 gap-y-1" data-testid="chat-who">
          <Link href={agentHref(agent.project, agent.handle)} className={`${NAME_LINK} font-mono text-[13px] font-normal`} title={t("agentPage", { name: agent.name })} data-testid="chat-agent-link">
            @{agent.handle}
          </Link>
          <span className="inline-flex min-w-0 items-center gap-1" data-testid="chat-agent-project">
            <FolderKanban className="size-3.5 shrink-0" aria-hidden="true" />
            <span className="sr-only">{t("project")}</span>
            <span className="truncate">{agent.project}</span>
          </span>
        </span>
      }
      testId="chat-header"
    />
  );
}
