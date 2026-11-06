"use client";

import { ChatView } from "@/components/chats/chat-view";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { AGENT_PANELS } from "./agent-panels";
import { useChatDecisions } from "./use-chat-decisions";

/**
 * The chat, with the decisions its agents' runs asked shown under the message that ended their turn, and the sheet an
 * agent's menu opens on its workers and pools or its accounts.
 */
export function ChatWithDecisions({ chatId, initialError }: { chatId: number; initialError: ApiErrorInfo | null }) {
  return <ChatView chatId={chatId} initialError={initialError} useUnder={useChatDecisions} agentPanels={AGENT_PANELS} />;
}
