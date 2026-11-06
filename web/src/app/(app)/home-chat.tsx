"use client";

import { ChatHome } from "@/components/chats/chat-home";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { AGENT_PANELS } from "./chats/[id]/agent-panels";
import { useChatDecisions } from "./chats/[id]/use-chat-decisions";

/**
 * The chat home with what a chat's page puts in it: the decisions an agent's run asked under the message that ended
 * its turn, and the agent sheet an agent's menu opens.
 */
export function HomeChat({ initialError, chatsOn }: { initialError: ApiErrorInfo | null; chatsOn: boolean }) {
  return <ChatHome initialError={initialError} chatsOn={chatsOn} useUnder={useChatDecisions} agentPanels={AGENT_PANELS} />;
}
