"use client";

import { Bot, FolderKanban, Info, Server, UserRoundCog, X } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { AgentAvatar } from "@/components/data/agent-avatar";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { agentHref } from "@/lib/agent-queries";

import { AGENT_TABS, type AgentTab, type AgentTarget } from "./agent-menu";

/**
 * What the page under src/app shows on each tab of the agent sheet, for the agent it is open on: the chat knows no
 * more of an agent than its face and name, so the page composes the agents' and the accounts' own views into it.
 */
export type AgentPanels = Record<AgentTab, (agent: AgentTarget) => ReactNode>;

/** Whether the agent is its project's default, which keeps every permission and owns no accounts. */
export function isDefaultAgent(agent: AgentTarget): boolean {
  return agent.is_default ?? agent.handle === agent.project;
}

const ICONS = { workers: Server, accounts: UserRoundCog } as const;

/**
 * The agent sheet of a chat (web/DESIGN.md, Chats), on the right, which the agent's menu opens on a tab: its face,
 * name, `@handle` and project with Agent page, then Workers and pools (where its runs may go) and Accounts (the
 * accounts it owns, which its runs take on the workers each is bound to), each drawn by the page (`panels`). The
 * project's default agent owns no accounts, and its tab says so.
 */
export function AgentSheet({
  target,
  panels,
  onTab,
  onOpenChange,
}: {
  target: { agent: AgentTarget; tab: AgentTab } | null;
  panels: AgentPanels;
  onTab: (tab: AgentTab) => void;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("chats.agentSheet");
  const agent = target?.agent ?? null;
  return (
    <Sheet open={target !== null} onOpenChange={onOpenChange}>
      <SheetContent side="right" showCloseButton={false} className="gap-0 p-0 data-[side=right]:w-full data-[side=right]:sm:max-w-xl" data-testid="agent-sheet" data-handle={agent?.handle}>
        {agent && target ? (
          <>
            <SheetHeader className="flex-row items-start gap-3 border-b p-4">
              <AgentAvatar agent={agent} size="lg" />
              <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                <SheetTitle className="truncate text-[15px] leading-[22px] font-semibold" title={agent.name}>
                  {agent.name}
                </SheetTitle>
                <SheetDescription className="flex min-w-0 flex-wrap items-center gap-x-1.5 text-[13px] leading-[18px]">
                  <span className="truncate font-mono">@{agent.handle}</span>
                  <FolderKanban className="size-3.5 shrink-0" aria-hidden="true" />
                  <span className="sr-only">{t("project")}</span>
                  <span className="truncate">{agent.project}</span>
                </SheetDescription>
              </div>
              <Button asChild variant="outline" size="sm" className="max-sm:min-w-11">
                <Link href={agentHref(agent.project, agent.handle)} data-testid="agent-sheet-page">
                  <Bot aria-hidden="true" />
                  <span className="max-sm:sr-only">{t("page")}</span>
                </Link>
              </Button>
              <Button type="button" variant="ghost" size="icon-sm" aria-label={t("close")} onClick={() => onOpenChange(false)}>
                <X aria-hidden="true" />
              </Button>
            </SheetHeader>
            <Tabs value={target.tab} onValueChange={(value) => onTab(value as AgentTab)} className="min-h-0 flex-1 gap-0">
              <TabsList className="px-4" aria-label={t("tabs", { name: agent.name })}>
                {AGENT_TABS.map((tab) => {
                  const Icon = ICONS[tab];
                  return (
                    <TabsTrigger key={tab} value={tab} data-testid={`agent-sheet-tab-${tab}`}>
                      <Icon aria-hidden="true" />
                      {t(`tab.${tab}`)}
                    </TabsTrigger>
                  );
                })}
              </TabsList>
              {AGENT_TABS.map((tab) => (
                <TabsContent key={tab} value={tab} className="min-h-0 flex-1 overflow-y-auto p-4" data-testid={`agent-sheet-${tab}`}>
                  {tab === "accounts" && isDefaultAgent(agent) ? <DefaultNote /> : panels[tab](agent)}
                </TabsContent>
              ))}
            </Tabs>
          </>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

function DefaultNote() {
  const t = useTranslations("chats.agentSheet");
  return (
    <p role="note" className="flex items-start gap-2.5 rounded-md border bg-surface-sunken px-4 py-3 text-[13px] text-pretty text-muted-foreground" data-testid="agent-sheet-default">
      <Info className="mt-px size-4 shrink-0" aria-hidden="true" />
      <span>{t("defaultAccounts")}</span>
    </p>
  );
}
