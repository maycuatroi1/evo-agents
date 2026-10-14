"use client";

import { useQuery } from "@tanstack/react-query";
import { Info } from "lucide-react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { agentsQuery } from "@/lib/agent-queries";
import { type Agent, usableAgents } from "@/lib/agents";
import { browserApi } from "@/lib/api/browser";

import type { AgentChoice } from "./agent-picker";

/**
 * The agent a run goes under, in the dispatch forms (Dispatch, Run plan, New plan, Revise with agent): the project's
 * agents that are not deleted, its default first, which is the choice until the person picks another. When the agents
 * cannot be read, the run goes under the default, as a dispatch that names none does, so a dispatch never waits on
 * them. `agent` is null until they are read (or when they could not be).
 */
export function useAgentChoice(project: string): AgentChoice {
  const state = useQuery(agentsQuery(browserApi, project));
  const agents = usableAgents(state.data ?? []);
  const [handle, choose] = useState<string | null>(null);
  const agent = agents.find((candidate) => candidate.handle === handle) ?? agents.find((candidate) => candidate.is_default) ?? null;
  return { agents, agent, choose, loading: state.isPending, failed: state.isError };
}

/**
 * The picker itself (agent-picker.tsx), whose code loads when a dispatch form opens: the forms load with the plans and
 * runs pages, and the picker's avatar and texts would otherwise count in each of those pages' First Load JS
 * (web/bundle-budget.json).
 */
export const AgentField = dynamic(() => import("./agent-picker").then((module) => module.AgentField), {
  ssr: false,
  loading: () => <Skeleton className="h-[86px] w-full" data-testid="dispatch-agent-loading" />,
});

/** The footer's line in place of the workers' outlook while the agent refuses what the form asks. */
export function AgentBlocked({ agent }: { agent: Agent }) {
  const t = useTranslations("runs.agent");
  return (
    <p className="flex min-w-0 flex-1 items-start gap-2 text-sm text-pretty text-danger" role="status" aria-live="polite" data-testid="dispatch-agent-blocked">
      <Info className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span>{t("blocked", { handle: agent.handle })}</span>
    </p>
  );
}
