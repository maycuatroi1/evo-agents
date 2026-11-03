"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";

import { agentPoolQuery } from "@/lib/agent-queries";
import type { Agent } from "@/lib/agents";
import { browserApi } from "@/lib/api/browser";

/**
 * Where an agent's runs go, for an admin: its own pool and the nodes in it, and its account pool. A run of an agent
 * goes to a node of that pool, never to a machine named by hand.
 */
export function AgentPlacement({ agent }: { agent: Agent }) {
  const t = useTranslations("agents.placement");
  const pool = agent.home_pool;
  const nodes = useQuery({ ...agentPoolQuery(browserApi, pool ?? ""), enabled: pool !== null });
  return (
    <section className="flex flex-col gap-2 rounded-md border px-3 py-3 sm:px-4" data-testid="agent-placement">
      <h3 className="text-sm font-medium">{t("title")}</h3>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-muted-foreground">{t("pool")}</dt>
        <dd className="font-mono text-xs" data-testid="agent-placement-pool">{pool ?? t("none")}</dd>
        <dt className="text-muted-foreground">{t("nodes")}</dt>
        <dd data-testid="agent-placement-nodes">
          {pool === null ? t("none") : nodes.data ? (nodes.data.nodes.length === 0 ? t("noNodes") : nodes.data.nodes.map((node) => node.name).join(", ")) : nodes.isError ? t("nodesFailed") : "…"}
        </dd>
        <dt className="text-muted-foreground">{t("accountPool")}</dt>
        <dd className="font-mono text-xs" data-testid="agent-placement-account-pool">{agent.account_pool ?? t("none")}</dd>
      </dl>
    </section>
  );
}
