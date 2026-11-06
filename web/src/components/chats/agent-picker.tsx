"use client";

import { ChevronsUpDown } from "lucide-react";
import { useTranslations } from "next-intl";

import { AgentAvatar } from "@/components/data/agent-avatar";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { ChatHomeAgent } from "@/lib/chat-keys";
import { cn } from "@/lib/utils";

/** The key that names one agent in the picker: handles are unique on the hub, the project goes with it for the eye. */
export function agentKey(agent: Pick<ChatHomeAgent, "handle">): string {
  return agent.handle;
}

/**
 * The agent picker of the chat frame (web/DESIGN.md, Chat home): a button with the chosen agent's face, name and
 * project that opens a menu of the agents the visitor may chat with, one radio item each (face, name, @handle and
 * project), in the hub's order (the ones written to last first). Choosing one calls `onPick` with its handle.
 */
export function AgentPicker({
  agents,
  current,
  onPick,
  className,
}: {
  agents: readonly ChatHomeAgent[];
  current: ChatHomeAgent;
  onPick: (handle: string) => void;
  className?: string;
}) {
  const t = useTranslations("chatHome.picker");
  return (
    // Not modal: the page under the menu stays in the accessibility tree, as in the chat's other menus.
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          className={cn("h-auto min-h-11 max-w-full justify-start gap-2.5 px-2 py-1.5 text-left", className)}
          aria-label={t("label", { name: current.name, project: current.project })}
          data-testid="chat-home-picker"
          data-agent={current.handle}
        >
          <AgentAvatar agent={current} />
          <span className="flex min-w-0 flex-col">
            <span className="truncate text-sm font-semibold text-foreground">{current.name}</span>
            <span className="truncate font-mono text-xs font-normal text-muted-foreground">
              @{current.handle}, {current.project}
            </span>
          </span>
          <ChevronsUpDown className="ml-1 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="max-h-[min(28rem,70dvh)] w-[min(22rem,calc(100vw-2rem))] overflow-y-auto" data-testid="chat-home-picker-menu">
        <DropdownMenuLabel className="text-xs font-medium text-muted-foreground">{t("heading", { count: agents.length })}</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={agentKey(current)} onValueChange={onPick}>
          {agents.map((agent) => (
            <DropdownMenuRadioItem
              key={agentKey(agent)}
              value={agentKey(agent)}
              className="min-h-11 gap-2.5 py-1.5 pr-8 pl-2"
              data-testid="chat-home-agent"
              data-agent={agent.handle}
            >
              <AgentAvatar agent={agent} size="sm" />
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="truncate text-sm">{agent.name}</span>
                <span className="truncate font-mono text-xs text-muted-foreground">
                  @{agent.handle}, {agent.project}
                </span>
              </span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
