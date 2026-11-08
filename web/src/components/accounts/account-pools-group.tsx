"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Layers, Plus, Trash2, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useMemo, useState } from "react";

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
import { allAgentAccountsQuery } from "./agent-queries";
import { signInAgainOn401, useFailure } from "./hooks";
import { type Provider, ProviderLogo } from "./provider-logo";
import { accountListQuery } from "./queries";

const POOL_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$/;
const LOGOS = new Set<string>(["claude", "anthropic", "openai", "gitlab", "github", "notion", "lark", "ssh"]);
const logoOf = (provider: string): Provider => (LOGOS.has(provider) ? (provider as Provider) : "other");

/** An account a pool may take, as the add menu lists it: the visitor's own, or an agent's of the project. */
type Candidate = { name: string; agent: string | null; provider: string };
const candidateKey = (candidate: { name: string; agent: string | null }) => `${candidate.agent ?? ""}/${candidate.name}`;

/** A write on a pool: it reloads the pools and the agents of the project whatever the hub answered. */
function usePoolWrite<T>(project: string, write: (variables: T) => Promise<unknown>) {
  const queryClient = useQueryClient();
  const failureOf = useFailure();
  const t = useTranslations("accounts.groups.pools");
  return useMutation({
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
}

/**
 * The accounts a pool of `project` may take: the visitor's model accounts, then the model accounts of the project's
 * agents, by name. A pool takes a name once, so the names it holds are left out.
 */
function useCandidates(project: string) {
  const mine = useQuery(accountListQuery(browserApi));
  const agents = useQuery(allAgentAccountsQuery(browserApi));
  return useMemo(() => {
    const own: Candidate[] = (mine.data ?? [])
      .filter((account) => account.category === "model")
      .map((account) => ({ name: account.name, agent: null, provider: account.provider }));
    const theirs: Candidate[] = (agents.data ?? [])
      .filter((account) => account.project === project && account.category === "model")
      .map((account) => ({ name: account.name, agent: account.handle, provider: account.provider }));
    const byName = (a: Candidate, b: Candidate) => a.name.localeCompare(b.name);
    return { own: own.sort(byName), theirs: theirs.sort(byName) };
  }, [mine.data, agents.data, project]);
}

function PoolCard({ project, pool, candidates }: { project: string; pool: AccountPool; candidates: ReturnType<typeof useCandidates> }) {
  const t = useTranslations("accounts.groups.pools");
  const add = usePoolWrite(project, (v: Candidate) => addPoolAccount(browserApi(), project, pool.name, v.name, v.agent));
  const remove = usePoolWrite(project, (id: number) => removePoolAccount(browserApi(), project, pool.name, id));
  const drop = usePoolWrite(project, () => deleteAccountPool(browserApi(), project, pool.name));
  const held = new Set(pool.accounts.map((account) => account.name));
  const own = candidates.own.filter((candidate) => !held.has(candidate.name));
  const theirs = candidates.theirs.filter((candidate) => !held.has(candidate.name));
  const inUse = pool.agents.length > 0;
  const onPick = (key: string) => {
    const picked = [...own, ...theirs].find((candidate) => candidateKey(candidate) === key);
    if (!picked) return;
    add.mutate(picked, { onSuccess: () => notify({ tone: "success", text: t("added", { name: picked.name, pool: pool.name }) }) });
  };
  return (
    <li className="flex min-w-0 flex-col rounded-lg border bg-card" data-testid={`account-pool-${project}-${pool.name}`}>
      <header className="flex items-start gap-2 border-b px-3 py-2.5">
        <Layers className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <h4 className="font-mono text-sm font-semibold [overflow-wrap:anywhere]">{pool.name}</h4>
          <p className="text-xs text-muted-foreground" data-testid="account-pool-used-by">
            {inUse ? t("usedBy", { agents: pool.agents.map((handle) => `@${handle}`).join(", ") }) : t("unused")}
          </p>
        </div>
        <Button
          variant="ghost"
          size="icon-sm"
          className="shrink-0 text-muted-foreground hover:text-danger"
          disabled={inUse || drop.isPending}
          title={inUse ? t("inUse") : t("deleteLabel", { pool: pool.name })}
          aria-label={t("deleteLabel", { pool: pool.name })}
          onClick={() => drop.mutate()}
          data-testid="account-pool-delete"
        >
          <Trash2 aria-hidden="true" />
        </Button>
      </header>
      {pool.accounts.length === 0 ? (
        <p className="px-3 py-3 text-sm text-fg-subtle">{t("noAccounts")}</p>
      ) : (
        <ul className="flex flex-col divide-y" aria-label={t("accountsOf", { pool: pool.name })}>
          {pool.accounts.map((account) => (
            <li key={account.id} className="flex min-h-11 items-center gap-2.5 px-3 py-1.5 text-sm" data-testid="account-pool-account">
              <ProviderLogo provider={logoOf(account.provider)} className="size-4" />
              <span className="min-w-0 flex-1 font-mono [overflow-wrap:anywhere]">{account.name}</span>
              {account.agent ? (
                <Badge variant="outline" className="shrink-0 font-mono">
                  @{account.agent}
                </Badge>
              ) : null}
              {account.paused ? <Badge variant="secondary">{t("paused")}</Badge> : null}
              <Button
                variant="ghost"
                size="icon-sm"
                className="shrink-0 text-muted-foreground"
                disabled={remove.isPending}
                aria-label={t("remove", { name: account.name, pool: pool.name })}
                title={t("remove", { name: account.name, pool: pool.name })}
                onClick={() => remove.mutate(account.id)}
                data-testid="account-pool-remove"
              >
                <X aria-hidden="true" />
              </Button>
            </li>
          ))}
        </ul>
      )}
      <div className="mt-auto border-t px-3 py-2.5">
        <NativeSelect
          size="sm"
          value=""
          disabled={add.isPending || (own.length === 0 && theirs.length === 0)}
          aria-label={t("addTo", { pool: pool.name })}
          onChange={(event) => onPick(event.target.value)}
          className="w-full"
          data-testid="account-pool-add-account"
        >
          <option value="">{own.length || theirs.length ? t("addAccount") : t("nothingToAdd")}</option>
          {own.length ? (
            <optgroup label={t("yours")}>
              {own.map((candidate) => (
                <option key={candidateKey(candidate)} value={candidateKey(candidate)}>
                  {candidate.name}
                </option>
              ))}
            </optgroup>
          ) : null}
          {theirs.length ? (
            <optgroup label={t("agents")}>
              {theirs.map((candidate) => (
                <option key={candidateKey(candidate)} value={candidateKey(candidate)}>
                  {`${candidate.name} (@${candidate.agent})`}
                </option>
              ))}
            </optgroup>
          ) : null}
        </NativeSelect>
      </div>
    </li>
  );
}

function NewPool({ project, taken, onDone }: { project: string; taken: readonly string[]; onDone: () => void }) {
  const t = useTranslations("accounts.groups.pools");
  const [name, setName] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const create = usePoolWrite(project, (value: string) => createAccountPool(browserApi(), project, value));
  const onCreate = (event: FormEvent) => {
    event.preventDefault();
    const value = name.trim();
    if (!POOL_NAME.test(value)) return setProblem(t("nameInvalid"));
    if (taken.some((item) => item.toLowerCase() === value.toLowerCase())) return setProblem(t("nameTaken", { name: value }));
    create.mutate(value, { onSuccess: onDone });
  };
  return (
    <form onSubmit={onCreate} className="flex flex-col gap-1.5 rounded-lg border border-dashed p-3" data-testid="account-pool-new">
      <div className="flex flex-wrap items-center gap-2">
        <Input
          autoFocus
          className="h-8 min-w-0 flex-1 font-mono placeholder:font-sans sm:max-w-64"
          value={name}
          maxLength={40}
          onChange={(event) => {
            setName(event.target.value);
            setProblem(null);
          }}
          onKeyDown={(event) => event.key === "Escape" && onDone()}
          placeholder={t("poolName")}
          aria-label={t("poolName")}
          aria-invalid={problem ? true : undefined}
          data-testid="account-pool-name"
        />
        <Button type="submit" size="sm" busy={create.isPending} disabled={!name.trim()} data-testid="account-pool-create">
          {t("create")}
        </Button>
        <Button type="button" variant="ghost" size="sm" onClick={onDone}>
          {t("cancel")}
        </Button>
      </div>
      {problem ? (
        <p className="text-xs text-danger" role="alert">
          {problem}
        </p>
      ) : null}
    </form>
  );
}

function AgentPools({ project, pools }: { project: string; pools: AccountPool[] }) {
  const t = useTranslations("accounts.groups.pools");
  const agents = useQuery(agentsQuery(browserApi, project));
  const attach = usePoolWrite(project, (v: { handle: string; pool: string | null }) => attachAccountPool(browserApi(), project, v.handle, v.pool));
  if (!agents.data || agents.data.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      <h4 className="text-xs font-medium text-muted-foreground">{t("agentsTitle")}</h4>
      <ul className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3" aria-label={t("agentsTitle")}>
        {agents.data.map((agent) => (
          <li key={agent.handle} className="flex min-w-0 items-center gap-2 rounded-md border px-3 py-2 text-sm">
            <span className="min-w-0 flex-1 font-mono [overflow-wrap:anywhere]">@{agent.handle}</span>
            <NativeSelect
              size="sm"
              value={agent.account_pool ?? ""}
              disabled={attach.isPending}
              aria-label={t("attachFor", { handle: agent.handle })}
              onChange={(event) => attach.mutate({ handle: agent.handle, pool: event.target.value || null })}
              className="w-40 shrink-0"
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
        ))}
      </ul>
    </div>
  );
}

function ProjectPools({ project }: { project: string }) {
  const t = useTranslations("accounts.groups.pools");
  const query = useQuery(accountPoolsQuery(browserApi, project));
  const candidates = useCandidates(project);
  const [adding, setAdding] = useState(false);
  const pools = query.data ?? [];
  return (
    <section className="flex flex-col gap-3 p-4" aria-labelledby={`account-pools-${project}-title`} data-testid={`account-pools-${project}`}>
      <header className="flex flex-wrap items-center gap-2">
        <h3 id={`account-pools-${project}-title`} className="font-mono text-sm font-semibold">
          {project}
        </h3>
        <Badge variant="secondary">{t("poolCount", { count: pools.length })}</Badge>
        {adding ? null : (
          <Button variant="outline" size="sm" className="ml-auto" onClick={() => setAdding(true)} data-testid="account-pool-open-new">
            <Plus aria-hidden="true" />
            {t("newPool")}
          </Button>
        )}
      </header>
      {adding ? <NewPool project={project} taken={pools.map((pool) => pool.name)} onDone={() => setAdding(false)} /> : null}
      {query.isError ? (
        <p className="text-sm text-danger" role="alert">
          {t("loadFailed")}
        </p>
      ) : pools.length === 0 && !query.isPending && !adding ? (
        <p className="rounded-md border border-dashed px-3 py-3 text-sm text-fg-subtle">{t("none")}</p>
      ) : (
        <ul className="grid min-w-0 gap-3 md:grid-cols-2 2xl:grid-cols-3">
          {pools.map((pool) => (
            <PoolCard key={pool.id} project={project} pool={pool} candidates={candidates} />
          ))}
        </ul>
      )}
      <AgentPools project={project} pools={pools} />
    </section>
  );
}

/**
 * The account pools of the projects the visitor administers (or every project, for a hub admin), a section per
 * project: its pools as cards, each with its accounts (a menu adds one of the visitor's model accounts or of the
 * project's agents, an X removes one) and the agents that use it; New pool; then which pool each agent of the project
 * runs on. Nothing shows for anyone else.
 */
export function AccountPoolsGroup() {
  const t = useTranslations("accounts.groups.pools");
  const me = useQuery(whoamiQuery(browserApi));
  const projects = me.data?.grants.filter((grant) => grant.role === "admin").map((grant) => grant.project) ?? [];
  if (projects.length === 0) return null;
  return (
    <AccountGroup id="pools" count={projects.length}>
      <div className="flex flex-col divide-y">
        {projects.map((project) => (
          <ProjectPools key={project} project={project} />
        ))}
        <p className="px-4 py-3 text-xs text-fg-muted">{t("hint")}</p>
      </div>
    </AccountGroup>
  );
}
