"use client";

import { LazyAgentAccountsCard } from "@/components/accounts/agent-accounts-lazy";
import { LazyAgentWorkersPanel } from "@/components/agents/agent-workers-lazy";
import type { AgentPanels } from "@/components/chats/agent-sheet";

/**
 * The agent sheet's tabs: where the agent's runs may go (the agents feature) and its accounts (the accounts feature). A
 * chat's page and the chat home both open them from an agent's menu.
 */
export const AGENT_PANELS: AgentPanels = {
  workers: (agent) => <LazyAgentWorkersPanel key={`${agent.project}/${agent.handle}`} project={agent.project} handle={agent.handle} />,
  accounts: (agent) => <LazyAgentAccountsCard key={`${agent.project}/${agent.handle}`} project={agent.project} handle={agent.handle} />,
};
