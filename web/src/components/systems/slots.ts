import type { ComponentType } from "react";

import type { Agent } from "@/lib/agents";
import type { ChatHomeAgent } from "@/lib/chat-keys";

/**
 * What the designer of an agent system shows from other areas of the web: an agent's config (the Agents pages), the
 * New agent form, and the chat with an agent (the chat home's frame, with its interactive answers). A feature folder
 * does not import another, so the route hands them in (`app/(app)/p/[project]/systems/[slug]/designer-slots.tsx`).
 */
export type AgentPanelProps = { project: string; agent: Agent; canChange: boolean; onSaved: (agent: Agent) => void };
export type NewAgentProps = { project: string; open: boolean; onOpenChange: (open: boolean) => void; onSaved: (agent: Agent) => void };
export type TestChatProps = { agent: ChatHomeAgent };

export type DesignerSlots = {
  AgentPanel: ComponentType<AgentPanelProps>;
  NewAgent: ComponentType<NewAgentProps>;
  TestChat: ComponentType<TestChatProps>;
};
