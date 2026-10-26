"use client";

import { Bot, FolderKanban, Server, UserRoundCog } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { createContext, type ReactNode, useContext } from "react";

import { AgentAvatar } from "@/components/data/agent-avatar";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { agentHref } from "@/lib/agent-queries";
import type { ChatAgent } from "@/lib/chats";
import { cn } from "@/lib/utils";

/** The tabs of the agent sheet: where its runs may go, and the accounts it owns. */
export type AgentTab = "workers" | "accounts";
export const AGENT_TABS: readonly AgentTab[] = ["workers", "accounts"];

/**
 * An agent of the chat as its menu and sheet need it: who it is, and whether it is its project's default (which owns
 * no accounts) or deleted (which takes no new run, so nothing of it changes). A message's author knows no default.
 */
export type AgentTarget = Pick<ChatAgent, "project" | "handle" | "name" | "color" | "icon_url" | "deleted"> & { is_default?: boolean };

/** Opens the agent sheet on a tab; null where the page has no sheet (a message drawn elsewhere). */
export const AgentActionsContext = createContext<((agent: AgentTarget, tab: AgentTab) => void) | null>(null);

/**
 * The faces and names in a chat that open the menu: a round focus ring on a face, a rounded one on a name. A face is
 * named for what it opens; a name keeps its own text as its name, so a heading around it still reads as the name.
 */
const TRIGGER = "cursor-pointer outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card";

/**
 * An agent's menu in a chat (web/DESIGN.md, Chats): pressing its face or its name, in the head, in the members or on a
 * message, opens who it is (its face, name, `@handle` and project), Agent page, and while it is not deleted Workers
 * and pools and Accounts, which open the agent sheet on that tab. Where the page has no sheet (`AgentActionsContext`
 * unset) the face or name is drawn as it was, with no button.
 */
export function AgentMenu({
  agent,
  children,
  shape = "round",
  align = "start",
  className,
  testId = "agent-menu-open",
}: {
  agent: AgentTarget;
  children: ReactNode;
  shape?: "round" | "text";
  align?: "start" | "end";
  className?: string;
  testId?: string;
}) {
  const t = useTranslations("chats.agentMenu");
  const open = useContext(AgentActionsContext);
  if (!open) return <>{children}</>;
  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={shape === "round" ? t("open", { name: agent.name }) : undefined}
          className={cn(TRIGGER, shape === "round" ? "shrink-0 rounded-full" : "max-w-full min-w-0 rounded-xs text-left hover:underline", className)}
          data-testid={testId}
          data-handle={agent.handle}
        >
          {children}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align={align} className="w-72" data-testid="agent-menu" data-handle={agent.handle}>
        <DropdownMenuLabel className="flex min-w-0 items-center gap-2.5 py-2 font-normal">
          <AgentAvatar agent={agent} />
          <span className="flex min-w-0 flex-col">
            <span className="truncate text-sm font-medium text-foreground">{agent.name}</span>
            <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
              <span className="truncate font-mono">@{agent.handle}</span>
              <FolderKanban className="size-3 shrink-0" aria-hidden="true" />
              <span className="sr-only">{t("project")}</span>
              <span className="truncate">{agent.project}</span>
            </span>
          </span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild data-testid="agent-menu-page">
          <Link href={agentHref(agent.project, agent.handle)}>
            <Bot aria-hidden="true" />
            {t("page")}
          </Link>
        </DropdownMenuItem>
        {agent.deleted ? null : (
          <>
            <DropdownMenuItem onSelect={() => open(agent, "workers")} data-testid="agent-menu-workers">
              <Server aria-hidden="true" />
              {t("workers")}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => open(agent, "accounts")} data-testid="agent-menu-accounts">
              <UserRoundCog aria-hidden="true" />
              {t("accounts")}
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
