"use client";

import { LazyAgentAccountsCard } from "@/components/accounts/agent-accounts-lazy";
import { LazyAgentWorkersPanel } from "@/components/agents/agent-workers-lazy";
import type { AgentPanels } from "@/components/chats/agent-sheet";
import { ChatView } from "@/components/chats/chat-view";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { useChatDecisions } from "./use-chat-decisions";

/** The agent sheet's tabs: where the agent's runs may go (the agents feature) and its accounts (the accounts feature). */
const AGENT_PANELS: AgentPanels = {
  workers: (agent) => <LazyAgentWorkersPanel key={`${agent.project}/${agent.handle}`} project={agent.project} handle={agent.handle} />,
  accounts: (agent) => <LazyAgentAccountsCard key={`${agent.project}/${agent.handle}`} project={agent.project} handle={agent.handle} />,
};

/**
 * The chat, with the decisions its agents' runs asked shown under the message that ended their turn, and the sheet an
 * agent's menu opens on its workers and pools or its accounts.
 */
export function ChatWithDecisions({ chatId, initialError }: { chatId: number; initialError: ApiErrorInfo | null }) {
  return <ChatView chatId={chatId} initialError={initialError} useUnder={useChatDecisions} agentPanels={AGENT_PANELS} />;
}
