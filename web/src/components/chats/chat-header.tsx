"use client";

import { ArrowLeft, Users } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";

import { AgentAvatar } from "@/components/data/agent-avatar";
import { VisibilityTag } from "@/components/data/visibility";
import { PageHeader } from "@/components/shell/page-header";
import { Button } from "@/components/ui/button";
import { type Chat, chatsHref } from "@/lib/chats";

/** The levels of a chat's label, each once, as the hub sends them: every member clears them all. */
function levelsOf(chat: Pick<Chat, "label">): string[] {
  const levels = chat.label.map((label) => (typeof label.level === "string" ? label.level : null)).filter((level): level is string => level !== null);
  return [...new Set(levels)];
}

/**
 * The head of a chat (web/DESIGN.md, Chats): its title as the page's h1, the visibility its agents' projects give it,
 * its agents' faces and who is in it, and Members, which opens the sheet that adds and removes them. Under 1024 px a
 * back link to the list stands before the title, since the list is a page of its own there.
 */
export function ChatHeader({ chat, onMembers }: { chat: Chat; onMembers: () => void }) {
  const t = useTranslations("chats.header");
  const format = useFormatter();
  const agents = chat.agents.filter((agent) => !agent.deleted);
  const names = [...agents.map((agent) => `@${agent.handle}`), ...chat.people.map((person) => person.login)];
  const shown = names.slice(0, 4);
  const who = names.length > shown.length ? t("andMore", { names: shown.join(", "), count: names.length - shown.length }) : format.list(shown, { type: "conjunction" });
  return (
    <div className="flex items-start gap-2 border-b px-3 py-3 sm:px-4">
      <Button asChild variant="ghost" size="icon-sm" className="-ml-1 lg:hidden">
        <Link href={chatsHref()} aria-label={t("back")} data-testid="chat-back">
          <ArrowLeft aria-hidden="true" />
        </Link>
      </Button>
      <PageHeader
        className="min-w-0 flex-1"
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
    </div>
  );
}
