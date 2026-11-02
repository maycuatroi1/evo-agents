"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";

import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { agentPoolQuery } from "@/lib/agent-queries";
import type { Agent } from "@/lib/agents";
import { browserApi } from "@/lib/api/browser";

import { useAgentFailure, useSetPlacement } from "./hooks";

/**
 * Where an agent's runs go, for an admin: its own pool and the nodes in it, its placement mode and its account pool.
 * A run of an agent in pool mode goes to a node of that pool, never to a machine named by hand; a legacy agent still
 * goes to the workers its worker axis lets through until an admin switches it to pool mode here.
 */
export function AgentPlacement({ project, agent, editable }: { project: string; agent: Agent; editable: boolean }) {
  const t = useTranslations("agents.placement");
  const pool = agent.home_pool;
  const nodes = useQuery({ ...agentPoolQuery(browserApi, pool ?? ""), enabled: pool !== null });
  const set = useSetPlacement(project, agent.handle);
  const failureOf = useAgentFailure();
  const isPool = agent.placement === "pool";

  const switchToPool = async () => {
    try {
      await set.mutateAsync("pool");
    } catch (error) {
      notify({ tone: "error", text: failureOf(error, {}).text });
      return;
    }
    notify({ tone: "success", text: t("switched", { handle: agent.handle }) });
  };

  return (
    <section className="flex flex-col gap-2 rounded-md border px-3 py-3 sm:px-4" data-testid="agent-placement" data-mode={agent.placement}>
      <h3 className="text-sm font-medium">{t("title")}</h3>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-muted-foreground">{t("mode")}</dt>
        <dd data-testid="agent-placement-mode">{isPool ? t("modePool") : t("modeLegacy")}</dd>
        <dt className="text-muted-foreground">{t("pool")}</dt>
        <dd className="font-mono text-xs" data-testid="agent-placement-pool">{pool ?? t("none")}</dd>
        <dt className="text-muted-foreground">{t("nodes")}</dt>
        <dd data-testid="agent-placement-nodes">
          {pool === null ? t("none") : nodes.data ? (nodes.data.nodes.length === 0 ? t("noNodes") : nodes.data.nodes.map((node) => node.name).join(", ")) : nodes.isError ? t("nodesFailed") : "…"}
        </dd>
        <dt className="text-muted-foreground">{t("accountPool")}</dt>
        <dd className="font-mono text-xs" data-testid="agent-placement-account-pool">{agent.account_pool ?? t("none")}</dd>
      </dl>
      {isPool ? null : <p className="text-xs text-pretty text-muted-foreground">{t("legacyNote")}</p>}
      {editable && !isPool ? (
        <div className="flex justify-end">
          <Button type="button" size="sm" busy={set.isPending} onClick={() => void switchToPool()} data-testid="agent-placement-switch">
            {t("switch")}
          </Button>
        </div>
      ) : null}
    </section>
  );
}
