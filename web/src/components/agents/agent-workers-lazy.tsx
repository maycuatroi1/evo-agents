"use client";

import dynamic from "next/dynamic";

import { Skeleton } from "@/components/ui/skeleton";

/**
 * The agent's Workers and pools panel loads when a chat's agent sheet first shows it, so its form, the axis field and
 * the options they read stay out of the chat's first load; a skeleton holds its place.
 */
export const LazyAgentWorkersPanel = dynamic(() => import("./agent-workers-panel").then((module) => module.AgentWorkersPanel), {
  ssr: false,
  loading: () => <Skeleton className="h-56 w-full rounded-md" />,
});
