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

import { AgentMenu } from "./agent-menu";
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
 * agent's, laid out as a messenger's: its avatar, its name on one line with Direct, under the name its @handle linking
 * to the agent's page, its project and the visibility, and Details at the end of the name's row, which opens the same
 * sheet without adding or taking out anyone; its face and its name open the agent's menu (`AgentMenu`), as a group
 * chat's agents' faces do; below 768 px Details and Direct are their icons, so the name, Direct and
 * Details share one row on a phone. Under 1024 px a back link to the list stands before the face, in the row of the face and the
 * title, so it stays beside them when the head wraps at 375 px.
 */
export function ChatHeader({ chat, me, onMembers }: { chat: Chat; me: string | null; onMembers: () => void }) {
  const [problem, setProblem] = useState<WriteFailure | null>(null);
  const agent = chat.kind === "direct" ? (chat.agents[0] ?? null) : null;
  return (
    <div className="flex flex-col gap-3 border-b px-3 py-3 sm:px-4" data-testid="chat-head" data-kind={chat.kind}>
      {agent ? (
        <DirectHead chat={chat} agent={agent} onDetails={onMembers} />
      ) : (
        <GroupHead chat={chat} me={me} onMembers={onMembers} image={<ChatImageControl chat={chat} me={me} onProblem={setProblem} />} />
      )}
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
  const names = [...agents.map((agent) => agent.name || agent.handle), ...chat.people.filter((person) => person.login.toLowerCase() !== me?.toLowerCase()).map((person) => person.login)];
  const shown = names.slice(0, 4);
  const who = names.length > shown.length ? t("andMore", { names: shown.join(", "), count: names.length - shown.length }) : names.length ? format.list(shown, { type: "conjunction" }) : t("onlyYou");
  return (
    <PageHeader
      className="min-w-0"
      leading={
        <>
          <BackLink />
          {image}
        </>
      }
      title={<span id="chat-title" data-testid="chat-title">{chat.title}</span>}
      tags={levelsOf(chat).map((level) => (
        <VisibilityTag key={level} level={level} testId="chat-visibility" />
      ))}
      actions={
        <>
          {agents.length ? (
            <span className="hidden -space-x-1.5 sm:flex" data-testid="chat-head-agents">
              {agents.slice(0, 4).map((agent) => (
                <AgentMenu key={`${agent.project}/${agent.handle}`} agent={agent} align="end">
                  <AgentAvatar agent={agent} size="sm" className="ring-2 ring-card" />
                </AgentMenu>
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
    <header className="flex min-w-0 items-start gap-2 md:gap-3" data-testid="chat-header">
      <div className="flex shrink-0 items-center gap-2 md:gap-3">
        <BackLink />
        <AgentMenu agent={agent}>
          <ChatAvatar face={{ kind: "agent", agent: agentAuthor(agent) }} size="lg" />
        </AgentMenu>
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <div className="flex min-h-11 min-w-0 items-center gap-2 md:min-h-10">
          <h1 className="min-w-0 truncate text-xl font-semibold tracking-tight" title={agent.name}>
            <AgentMenu agent={agent} shape="text" testId="agent-menu-name">
              <span id="chat-title" data-testid="chat-title">
                {agent.name}
              </span>
            </AgentMenu>
          </h1>
          <Tag data-testid="chat-direct-tag">
            <Bot aria-hidden="true" />
            <span className="max-md:sr-only">{t("direct")}</span>
          </Tag>
          {agent.deleted ? <Tag className="text-danger">{t("deleted")}</Tag> : null}
        </div>
        <p className="text-[13px] leading-[18px] text-muted-foreground [overflow-wrap:anywhere]" data-slot="page-sub">
          <span className="inline-flex max-w-full flex-wrap items-center gap-x-3 gap-y-1" data-testid="chat-who">
            <Link href={agentHref(agent.project, agent.handle)} className={`${NAME_LINK} max-w-full truncate font-mono text-[13px] font-normal`} title={t("agentPage", { name: agent.name })} data-testid="chat-agent-link">
              {agent.handle}
            </Link>
            <span className="inline-flex min-w-0 items-center gap-1" data-testid="chat-agent-project">
              <FolderKanban className="size-3.5 shrink-0" aria-hidden="true" />
              <span className="sr-only">{t("project")}</span>
              <span className="truncate">{agent.project}</span>
            </span>
            {levelsOf(chat).map((level) => (
              <VisibilityTag key={level} level={level} testId="chat-visibility" />
            ))}
          </span>
        </p>
      </div>
      <div className="flex h-11 shrink-0 items-center md:h-10">
        <Button type="button" variant="outline" size="sm" onClick={onDetails} className="max-md:min-w-11" data-testid="chat-details-open">
          <Info aria-hidden="true" />
          <span className="max-md:sr-only">{t("details")}</span>
        </Button>
      </div>
    </header>
  );
}
