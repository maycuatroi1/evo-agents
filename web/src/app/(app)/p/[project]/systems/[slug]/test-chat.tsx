"use client";

import type { TestChatProps } from "@/components/systems/slots";
import { AgentChat } from "@/components/chats/agent-chat";

import { AGENT_PANELS } from "../../../../chats/[id]/agent-panels";
import { useChatDecisions } from "../../../../chats/[id]/use-chat-decisions";

const NO_PICK = () => undefined;

/** The test chat is the chat home's frame with the entry agent alone: the same composer, threads and answers. */
export function TestChat({ agent }: TestChatProps) {
  return <AgentChat agents={[agent]} handle={agent.handle} onPick={NO_PICK} useUnder={useChatDecisions} agentPanels={AGENT_PANELS} className="h-full min-h-[24rem]" />;
}
