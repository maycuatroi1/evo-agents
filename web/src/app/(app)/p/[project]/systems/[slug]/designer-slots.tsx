"use client";

import dynamic from "next/dynamic";

import type { AgentPanelProps, DesignerSlots, NewAgentProps } from "@/components/systems/slots";
import { type SystemMode, SystemPage } from "@/components/systems/system-page";
import { Skeleton } from "@/components/ui/skeleton";
import type { ApiErrorInfo } from "@/lib/api/errors";

/**
 * What the designer borrows loads when it is used, not with the page: the config form when a node is selected, the
 * New agent form when it opens, the chat (with its decision cards and agent panels) when Test opens.
 */
const AgentConfigPanel = dynamic(() => import("@/components/agents/agent-config-panel").then((module) => module.AgentConfigPanel), {
  ssr: false,
  loading: () => <Skeleton className="h-64 w-full" />,
});
const AgentDialog = dynamic(() => import("@/components/agents/agent-dialog").then((module) => module.AgentDialog), { ssr: false });
const TestChat = dynamic(() => import("./test-chat").then((module) => module.TestChat), {
  ssr: false,
  loading: () => <Skeleton className="m-4 h-40" />,
});

function AgentPanel(props: AgentPanelProps) {
  return <AgentConfigPanel {...props} />;
}

function NewAgent({ project, open, onOpenChange, onSaved }: NewAgentProps) {
  if (!open) return null;
  return <AgentDialog project={project} agent={null} open={open} onOpenChange={onOpenChange} onSaved={onSaved} />;
}

const SLOTS: DesignerSlots = { AgentPanel, NewAgent, TestChat };

/**
 * The page of one agent system with what its designer borrows from other areas: the Agents config form and New agent
 * dialog, and the chat with an agent. Feature folders do not import one another, so the route joins them here.
 */
export function SystemRoute({ project, slug, mode, initialError }: { project: string; slug: string; mode: SystemMode; initialError: ApiErrorInfo | null }) {
  return <SystemPage project={project} slug={slug} mode={mode} initialError={initialError} slots={SLOTS} />;
}
