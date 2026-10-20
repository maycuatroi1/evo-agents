"use client";

import { MessagesSquare } from "lucide-react";
import { useTranslations } from "next-intl";

import { notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { useCanChat, useDirectChat } from "@/hooks/use-direct-chat";
import type { Agent } from "@/lib/agents";

import { useAgentFailure } from "./hooks";

/**
 * Chat, on the Agents pages (web/DESIGN.md, Agents): opens the visitor's direct chat with the agent, the one there is or
 * a new one, with the composer focused, as the agent's avatar in the Chats list does. Shown only where the hub would
 * open one: an agent not deleted, of a project the visitor writes to, while chats are on. `compact` is the table row's
 * icon button, named by the agent.
 */
export function ChatWithAgent({ agent, compact = false }: { agent: Pick<Agent, "handle" | "name" | "project" | "deleted_at">; compact?: boolean }) {
  const t = useTranslations("agents.chat");
  const failure = useAgentFailure();
  const allowed = useCanChat(agent.project);
  const direct = useDirectChat({ onError: (error, handle) => notifyFailure(t("failed", { handle }), failure(error)) });
  if (!allowed || agent.deleted_at !== null) return null;
  const busy = direct.opening === agent.handle;
  return (
    <Button
      type="button"
      variant="outline"
      size={compact ? "icon-sm" : "default"}
      busy={busy}
      onClick={() => direct.open(agent.handle)}
      aria-label={compact ? t("label", { name: agent.name }) : undefined}
      title={compact ? t("label", { name: agent.name }) : undefined}
      data-testid="agent-chat"
      data-handle={agent.handle}
    >
      <MessagesSquare aria-hidden="true" />
      {compact ? null : t("button")}
    </Button>
  );
}
