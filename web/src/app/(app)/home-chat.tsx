"use client";

import { ChatHome } from "@/components/chats/chat-home";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { AGENT_PANELS } from "./chats/[id]/agent-panels";
import { ChatWithDecisions } from "./chats/[id]/chat-with-decisions";
import { useChatDecisions } from "./chats/[id]/use-chat-decisions";
import { HomeRailFoot } from "./home-rail-foot";

const renderTask = (chatId: number) => <ChatWithDecisions key={chatId} chatId={chatId} initialError={null} />;

/**
 * The chat home with what a chat's page puts in it: the decisions an agent's run asked under the message that ended
 * its turn, the agent sheet an agent's menu opens, a task as its own page draws it (its work card, its decisions), and
 * the rail's foot with the palette, the Inbox, Today and the account.
 */
export function HomeChat({ initialError, chatsOn }: { initialError: ApiErrorInfo | null; chatsOn: boolean }) {
  return (
    <ChatHome
      initialError={initialError}
      chatsOn={chatsOn}
      useUnder={useChatDecisions}
      agentPanels={AGENT_PANELS}
      renderTask={renderTask}
      foot={<HomeRailFoot />}
    />
  );
}
