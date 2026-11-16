"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { notify, notifyFailure } from "@/components/feedback/toast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import { whoamiQuery } from "@/lib/queries";

import { type AccountPool, accountPoolKeys, type PoolAccount, setPoolPolicy } from "./account-pools-queries";
import { ZAI_PROVIDER } from "./budget-model";
import { signInAgainOn401, useFailure } from "./hooks";
import { CEILING_PCT, RESERVE_PCT } from "./model";
import { poolPolicyBody, type PoolPolicyForm, poolPolicyFormOf, poolPolicyProblems, poolUsage } from "./pool-model";
import { type Provider, ProviderLogo } from "./provider-logo";
import { STRATEGIES, type Strategy } from "./queries";
import { UsageLimits } from "./usage-limits";
import { ZaiRefresh } from "./zai-refresh";

const LOGOS = new Set<string>(["claude", "anthropic", "openai", "gitlab", "github", "notion", "lark", "ssh"]);
const logoOf = (provider: string): Provider => (LOGOS.has(provider) ? (provider as Provider) : "other");

/** A percent field of a pool's quota-pacing, small enough to sit beside the strategy. */
function PercentInput({ label, value, problem, onChange, testId }: { label: string; value: string; problem: boolean; onChange: (value: string) => void; testId: string }) {
  const id = useId();
  return (
    <label htmlFor={id} className="flex items-center gap-1.5 text-xs text-muted-foreground">
      <span>{label}</span>
      <Input
        id={id}
        type="text"
        inputMode="numeric"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        autoComplete="off"
        className="h-8 w-14 tabular-nums"
        aria-invalid={problem || undefined}
        data-testid={testId}
      />
      <span aria-hidden="true">%</span>
    </label>
  );
}

/**
 * How the pool picks among its accounts: its strategy and, under quota-pacing, the weekly reserve and the five-hour
 * ceiling. Save shows once something changed and sends only that.
 */
export function PoolPolicyEditor({ project, pool }: { project: string; pool: AccountPool }) {
  const t = useTranslations("accounts.groups.pools.policy");
  const tPolicy = useTranslations("accounts.policies");
  const failureOf = useFailure();
  const queryClient = useQueryClient();
  const [form, setForm] = useState<PoolPolicyForm>(() => poolPolicyFormOf(pool));
  const save = useMutation({
    mutationFn: (body: NonNullable<ReturnType<typeof poolPolicyBody>>) => setPoolPolicy(browserApi(), project, pool.name, body),
    onError: (error) => {
      signInAgainOn401(error);
      notifyFailure(t("failed", { pool: pool.name }), failureOf(error));
    },
    onSuccess: () => notify({ tone: "success", text: t("saved", { pool: pool.name }) }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: accountPoolKeys.of(project) }),
  });
  const problems = poolPolicyProblems(form);
  const body = poolPolicyBody(form, pool);
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (body && Object.keys(problems).length === 0) save.mutate(body);
  };
  const pacing = form.strategy === "quota-pacing";
  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-1.5 border-b px-3 py-2.5" data-testid="account-pool-policy" noValidate>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <NativeSelect
          size="sm"
          value={form.strategy}
          onChange={(event) => setForm((current) => ({ ...current, strategy: event.target.value as Strategy }))}
          aria-label={t("strategyOf", { pool: pool.name })}
          title={tPolicy(`strategyHint.${form.strategy}`)}
          className="min-w-0"
          data-testid="account-pool-strategy"
        >
          {STRATEGIES.map((strategy) => (
            <NativeSelectOption key={strategy} value={strategy}>
              {tPolicy(`strategy.${strategy}`)}
            </NativeSelectOption>
          ))}
        </NativeSelect>
        {pacing ? (
          <>
            <PercentInput
              label={t("reserve")}
              value={form.reservePct}
              problem={Boolean(problems.reservePct)}
              onChange={(reservePct) => setForm((current) => ({ ...current, reservePct }))}
              testId="account-pool-reserve"
            />
            <PercentInput
              label={t("ceiling")}
              value={form.ceilingPct}
              problem={Boolean(problems.ceilingPct)}
              onChange={(ceilingPct) => setForm((current) => ({ ...current, ceilingPct }))}
              testId="account-pool-ceiling"
            />
          </>
        ) : null}
        {body ? (
          <Button type="submit" size="sm" busy={save.isPending} disabled={Object.keys(problems).length > 0} data-testid="account-pool-policy-save">
            {t("save")}
          </Button>
        ) : null}
      </div>
      {pacing && Object.keys(problems).length > 0 ? (
        <p className="text-xs text-danger" role="alert">
          {t("problem", { reserve: `${RESERVE_PCT.min}-${RESERVE_PCT.max}`, ceiling: `${CEILING_PCT.min}-${CEILING_PCT.max}` })}
        </p>
      ) : null}
    </form>
  );
}

/**
 * One account of a pool: its logo, name, owning agent and pause, Remove, then its usage bars when the hub kept a
 * report (a weekly budget's among them) or it is a Z.ai account, whose quota Refresh reads again: the visitor's own
 * or an agent's of the project.
 */
export function PoolAccountItem({
  project,
  pool,
  account,
  removing,
  onRemove,
  pageNow,
}: {
  project: string;
  pool: string;
  account: PoolAccount;
  removing: boolean;
  onRemove: () => void;
  pageNow: number;
}) {
  const t = useTranslations("accounts.groups.pools");
  const me = useQuery(whoamiQuery(browserApi));
  const usage = poolUsage(account);
  const zai = account.provider === ZAI_PROVIDER && account.kind === "provider";
  const refreshable = zai && (account.agent !== null || (me.data !== undefined && account.owner === me.data.login));
  const action = refreshable ? <ZaiRefresh account={account.name} agent={account.agent ? { project, handle: account.agent } : undefined} /> : null;
  return (
    <li className="flex flex-col gap-2 px-3 py-1.5 text-sm" data-testid="account-pool-account">
      <div className="flex min-h-8 items-center gap-2.5">
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
          disabled={removing}
          aria-label={t("remove", { name: account.name, pool })}
          title={t("remove", { name: account.name, pool })}
          onClick={onRemove}
          data-testid="account-pool-remove"
        >
          <X aria-hidden="true" />
        </Button>
      </div>
      {usage || zai ? (
        <div className="pb-1.5 pl-6.5">
          <UsageLimits account={{ name: account.name, last_usage: usage, last_usage_at: null }} pageNow={pageNow} action={action} />
        </div>
      ) : null}
    </li>
  );
}
