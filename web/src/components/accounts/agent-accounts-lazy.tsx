"use client";

import dynamic from "next/dynamic";

import { TableSkeleton } from "@/components/states/states";

/**
 * The agent's Accounts card and the Agent accounts group of the Accounts page load after the page around them, so
 * their tables, usage bars and provider logos stay out of the page's first load; a skeleton holds their place.
 */
export const LazyAgentAccountsCard = dynamic(() => import("./agent-accounts-card").then((module) => module.AgentAccountsCard), {
  ssr: false,
  loading: () => <TableSkeleton rows={2} />,
});

export const LazyAgentAccountsGroup = dynamic(() => import("./agent-accounts-group").then((module) => module.AgentAccountsGroup), {
  ssr: false,
  loading: () => <TableSkeleton rows={2} />,
});
