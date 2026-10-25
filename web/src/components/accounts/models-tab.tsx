"use client";

import { useQueries, useQuery } from "@tanstack/react-query";
import { Boxes, Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { DataCard } from "@/components/data/data-card";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { projectsQuery } from "@/lib/queries";
import { runnableModelsQuery } from "@/lib/runnable-models";
import { useRuntimeName } from "@/lib/runtimes";

import { hasClaudeLogin, type ModelRow, mergeRunnable } from "./provider-cards";
import { ProviderMark } from "./provider-mark";
import { catalogQuery, gatewayStateQuery, providerAccountsQuery } from "./provider-queries";
import { GatewayNotice } from "./providers-tab";
import type { Account } from "./queries";

type Group = { prefix: string; provider: string; name: string; rows: ModelRow[] };

/** The state a model shows: ready while one account that serves it is, else the state of the first. */
function stateOf(row: ModelRow, states: ReadonlyMap<string, Account["state"]>): Account["state"] | "unknown" {
  const known = row.accounts.map((name) => states.get(name)).filter((state) => state !== undefined);
  return known.includes("ready") ? "ready" : (known[0] ?? "unknown");
}

function ModelLine({ row, state }: { row: ModelRow; state: Account["state"] | "unknown" }) {
  const t = useTranslations("accounts.provider.models");
  const tState = useTranslations("accounts.state");
  const runtimeName = useRuntimeName();
  return (
    <li className="flex flex-col gap-1.5 px-4 py-3 sm:flex-row sm:items-center sm:gap-4" data-testid="runnable-model" data-model={row.model}>
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="font-mono text-[13px] [overflow-wrap:anywhere]">{row.model}</span>
        <span className="text-xs text-fg-muted [overflow-wrap:anywhere]">
          {t("servedBy", { accounts: row.accounts.join(", "), projects: row.projects.join(", ") })}
        </span>
      </div>
      <ul className="flex flex-wrap gap-1.5" aria-label={t("runtimes")}>
        {row.runtimes.map((runtime) => (
          <li key={runtime}>
            <Badge variant="outline" data-testid="runnable-model-runtime">
              {runtimeName(runtime)}
            </Badge>
          </li>
        ))}
      </ul>
      <Badge variant={state === "ready" ? "success" : "secondary"} className="w-fit" data-testid="runnable-model-state" data-state={state}>
        {state === "unknown" ? t("unknown") : tState(state)}
      </Badge>
    </li>
  );
}

/**
 * The Models tab: every model a run of the visitor's projects may ask for, by provider, as the run names it
 * (`<prefix>/<model>`), with the runtimes that run it, the accounts and projects behind it and its state; then Claude
 * Code's own models when the visitor signs in to Claude. A model of a provider account runs through the hub's model
 * gateway, so a hub without one says so above the list.
 */
export function ModelsTab({ accounts, onCatalog }: { accounts: Account[]; onCatalog: () => void }) {
  const t = useTranslations("accounts.provider.models");
  const projects = useQuery(projectsQuery(browserApi));
  const names = useMemo(() => (projects.data ?? []).map((project) => project.name).sort(), [projects.data]);
  const lists = useQueries({ queries: names.map((project) => runnableModelsQuery(browserApi, project)) });
  const providers = useQuery(providerAccountsQuery(browserApi));
  const catalog = useQuery(catalogQuery(browserApi));
  const gateway = useQuery(gatewayStateQuery(browserApi));
  const pending = projects.isPending || lists.some((list) => list.isPending);
  const failed = projects.isError || lists.some((list) => list.isError);
  const loaded = lists.map((list, index) => ({ project: names[index], models: list.data ?? [] }));
  const rows = mergeRunnable(loaded);
  const states = new Map(accounts.map((account) => [account.name, account.state]));
  const byAlias = new Map((providers.data ?? []).map((provider) => [provider.alias, provider]));
  const entries = new Map((catalog.data?.providers ?? []).map((entry) => [entry.id, entry]));
  const groups: Group[] = [];
  for (const row of rows) {
    let group = groups.find((item) => item.prefix === row.prefix);
    if (!group) {
      const provider = byAlias.get(row.prefix);
      group = { prefix: row.prefix, provider: provider?.provider ?? row.prefix, name: provider?.provider_name ?? row.prefix, rows: [] };
      groups.push(group);
    }
    group.rows.push(row);
  }
  const claude = hasClaudeLogin(accounts);
  const gatewayState = gateway.data === "off" || gateway.data === "error" ? gateway.data : null;

  if (pending) return <TableSkeleton rows={3} />;
  if (failed) {
    return (
      <p className="text-sm text-danger" role="alert" data-testid="accounts-models-error">
        {t("loadFailed")}
      </p>
    );
  }
  if (groups.length === 0 && !claude) {
    return (
      <EmptyState icon={Boxes} title={t("emptyTitle")} description={t("emptyText")} testId="accounts-models-empty">
        <Button onClick={onCatalog} data-testid="accounts-models-add">
          <Plus aria-hidden="true" />
          {t("add")}
        </Button>
      </EmptyState>
    );
  }
  return (
    <div className="flex flex-col gap-4" data-testid="accounts-models">
      {groups.length > 0 && gatewayState ? <GatewayNotice state={gatewayState} /> : null}
      {groups.map((group) => (
        <section key={group.prefix} aria-labelledby={`models-${group.prefix}`} data-testid="runnable-models-group" data-prefix={group.prefix}>
          <DataCard
            toolbar={
              <header className="flex items-center gap-3 border-b px-4 py-3">
                <ProviderMark id={group.provider} entry={entries.get(group.provider) ?? null} size="sm" />
                <h3 id={`models-${group.prefix}`} className="min-w-0 text-sm font-semibold [overflow-wrap:anywhere]">
                  {group.name}
                </h3>
                <Badge variant="secondary">{t("count", { count: group.rows.length })}</Badge>
              </header>
            }
          >
            <ul className="divide-y">
              {group.rows.map((row) => (
                <ModelLine key={row.model} row={row} state={stateOf(row, states)} />
              ))}
            </ul>
          </DataCard>
        </section>
      ))}
      {claude ? (
        <section aria-labelledby="models-claude" data-testid="runnable-models-claude">
          <DataCard>
            <div className="flex items-start gap-3 px-4 py-3">
              <ProviderMark id="claude" entry={null} size="sm" />
              <div className="flex min-w-0 flex-col gap-0.5">
                <h3 id="models-claude" className="text-sm font-semibold">
                  {t("claudeTitle")}
                </h3>
                <p className="text-xs text-pretty text-fg-muted">{t("claudeText")}</p>
              </div>
            </div>
          </DataCard>
        </section>
      ) : null}
    </div>
  );
}
