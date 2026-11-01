"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type FormEvent, useState } from "react";

import { notify, notifyFailure } from "@/components/feedback/toast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { agentKeys, agentsQuery } from "@/lib/agent-queries";
import { browserApi } from "@/lib/api/browser";
import { whoamiQuery } from "@/lib/queries";

import { AccountGroup } from "./account-group";
import {
  type AccountPool,
  accountPoolKeys,
  accountPoolsQuery,
  addPoolAccount,
  attachAccountPool,
  createAccountPool,
  deleteAccountPool,
  removePoolAccount,
} from "./account-pools-queries";
import { signInAgainOn401, useFailure } from "./hooks";

/** A write on a pool: it reloads the pools and the agents of the project whatever the hub answered. */
function usePoolWrite<T>(project: string, write: (variables: T) => Promise<unknown>) {
  const queryClient = useQueryClient();
  const failureOf = useFailure();
  const t = useTranslations("accounts.groups.pools");
  const mutation = useMutation({
    mutationFn: write,
    onError: (error) => {
      signInAgainOn401(error);
      notifyFailure(t("failed"), failureOf(error));
    },
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: accountPoolKeys.of(project) }),
        queryClient.invalidateQueries({ queryKey: agentKeys.all(project) }),
      ]),
  });
  return mutation;
}

function PoolCard({ project, pool }: { project: string; pool: AccountPool }) {
  const t = useTranslations("accounts.groups.pools");
  const [name, setName] = useState("");
  const [agent, setAgent] = useState("");
  const add = usePoolWrite(project, (v: { name: string; agent: string | null }) => addPoolAccount(browserApi(), project, pool.name, v.name, v.agent));
  const remove = usePoolWrite(project, (id: number) => removePoolAccount(browserApi(), project, pool.name, id));
  const drop = usePoolWrite(project, () => deleteAccountPool(browserApi(), project, pool.name));
  const onAdd = (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim()) return;
    add.mutate(
      { name: name.trim(), agent: agent.trim() || null },
      {
        onSuccess: () => {
          setName("");
          setAgent("");
          notify({ tone: "success", text: t("added", { pool: pool.name }) });
        },
      },
    );
  };
  return (
    <li className="flex flex-col gap-2 rounded-md border p-3" data-testid={`account-pool-${project}-${pool.name}`}>
      <div className="flex items-center gap-2">
        <h3 className="font-mono text-sm font-medium">{pool.name}</h3>
        <Badge variant="secondary">{t("accountCount", { count: pool.accounts.length })}</Badge>
        <Badge variant="outline">{t("agentCount", { count: pool.agents.length })}</Badge>
        <Button
          variant="outline"
          size="sm"
          className="ml-auto"
          disabled={pool.agents.length > 0 || drop.isPending}
          title={pool.agents.length > 0 ? t("inUse") : undefined}
          onClick={() => drop.mutate()}
          data-testid="account-pool-delete"
        >
          {t("delete")}
        </Button>
      </div>
      {pool.accounts.length === 0 ? <p className="text-sm text-fg-subtle">{t("noAccounts")}</p> : null}
      <ul className="flex flex-col gap-1">
        {pool.accounts.map((account) => (
          <li key={account.id} className="flex items-center gap-2 text-sm" data-testid="account-pool-account">
            <span className="font-mono">{account.name}</span>
            <span className="text-fg-muted">{account.env_var ?? account.provider}</span>
            {account.agent ? <span className="text-fg-muted">@{account.agent}</span> : null}
            <Button variant="ghost" size="sm" className="ml-auto" disabled={remove.isPending} onClick={() => remove.mutate(account.id)}>
              {t("remove", { name: account.name })}
            </Button>
          </li>
        ))}
      </ul>
      <form onSubmit={onAdd} className="flex flex-wrap items-center gap-2">
        <Input className="w-48" value={name} onChange={(e) => setName(e.target.value)} placeholder={t("accountName")} aria-label={t("accountName")} />
        <Input className="w-40" value={agent} onChange={(e) => setAgent(e.target.value)} placeholder={t("agentHandle")} aria-label={t("agentHandle")} />
        <Button type="submit" size="sm" variant="outline" disabled={add.isPending || !name.trim()} data-testid="account-pool-add-account">
          {t("addAccount")}
        </Button>
      </form>
    </li>
  );
}

function AgentRow({ project, handle, current, pools }: { project: string; handle: string; current: string | null; pools: AccountPool[] }) {
  const t = useTranslations("accounts.groups.pools");
  const attach = usePoolWrite(project, (pool: string | null) => attachAccountPool(browserApi(), project, handle, pool));
  return (
    <li className="flex items-center gap-2 text-sm">
      <span className="font-mono">@{handle}</span>
      <NativeSelect
        size="sm"
        value={current ?? ""}
        disabled={attach.isPending}
        aria-label={t("attachFor", { handle })}
        onChange={(e) => attach.mutate(e.target.value || null)}
        data-testid="account-pool-attach"
      >
        <option value="">{t("noPool")}</option>
        {pools.map((pool) => (
          <option key={pool.id} value={pool.name}>
            {pool.name}
          </option>
        ))}
      </NativeSelect>
    </li>
  );
}

function ProjectPools({ project, pools }: { project: string; pools: AccountPool[] }) {
  const t = useTranslations("accounts.groups.pools");
  const [name, setName] = useState("");
  const agents = useQuery(agentsQuery(browserApi, project));
  const create = usePoolWrite(project, (value: string) => createAccountPool(browserApi(), project, value));
  const onCreate = (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim()) return;
    create.mutate(name.trim(), { onSuccess: () => setName("") });
  };
  return (
    <div className="flex flex-col gap-3" data-testid={`account-pools-${project}`}>
      <h3 className="text-sm font-semibold">{project}</h3>
      <form onSubmit={onCreate} className="flex items-center gap-2">
        <Input className="w-56" value={name} onChange={(e) => setName(e.target.value)} placeholder={t("poolName")} aria-label={t("poolName")} />
        <Button type="submit" size="sm" disabled={create.isPending || !name.trim()} data-testid="account-pool-create">
          {t("create")}
        </Button>
      </form>
      <ul className="flex flex-col gap-2">
        {pools.map((pool) => (
          <PoolCard key={pool.id} project={project} pool={pool} />
        ))}
      </ul>
      {agents.data && agents.data.length > 0 ? (
        <ul className="flex flex-col gap-1.5" aria-label={t("agents")}>
          {agents.data.map((agent) => (
            <AgentRow key={agent.handle} project={project} handle={agent.handle} current={agent.account_pool} pools={pools} />
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function AdminProject({ project, onCount }: { project: string; onCount: (pools: AccountPool[]) => React.ReactNode }) {
  const query = useQuery(accountPoolsQuery(browserApi, project));
  if (!query.data) return null;
  return <>{onCount(query.data)}</>;
}

/**
 * The account pools of the projects the visitor administers (or every project, for a hub admin): an admin creates a
 * pool, adds accounts to it and attaches it to the agents of the project. Nothing shows for anyone else.
 */
export function AccountPoolsGroup() {
  const t = useTranslations("accounts.groups.pools");
  const me = useQuery(whoamiQuery(browserApi));
  const projects = me.data?.grants.filter((grant) => grant.role === "admin").map((grant) => grant.project) ?? [];
  if (projects.length === 0) return null;
  return (
    <AccountGroup id="pools" count={projects.length}>
      <div className="flex flex-col gap-6 p-4">
        {projects.map((project) => (
          <AdminProject key={project} project={project} onCount={(pools) => <ProjectPools project={project} pools={pools} />} />
        ))}
        <p className="text-xs text-fg-muted">{t("hint")}</p>
      </div>
    </AccountGroup>
  );
}
