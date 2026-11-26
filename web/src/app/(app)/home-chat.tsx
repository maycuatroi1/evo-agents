"use client";

import { ChatHome } from "@/components/chats/chat-home";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { AGENT_PANELS } from "./chats/[id]/agent-panels";
import { useChatDecisions } from "./chats/[id]/use-chat-decisions";
import { HomeRailFoot } from "./home-rail-foot";
import { HomeTask } from "./home-task";

const renderTask = (chatId: number) => <HomeTask key={chatId} chatId={chatId} />;

/**
 * The chat home with what a chat's page puts in it: the decisions an agent's run asked under the message that ended
 * its turn, the agent sheet an agent's menu opens, a task as Home's task view draws it (`HomeTask`), and
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
