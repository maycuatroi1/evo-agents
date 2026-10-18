"use client";

import { ChatView } from "@/components/chats/chat-view";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { useChatDecisions } from "./use-chat-decisions";

/** The chat, with the decisions its agents' runs asked shown under the message that ended their turn. */
export function ChatWithDecisions({ chatId, initialError }: { chatId: number; initialError: ApiErrorInfo | null }) {
  return <ChatView chatId={chatId} initialError={initialError} useUnder={useChatDecisions} />;
}
