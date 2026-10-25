"use client";

import { useQuery } from "@tanstack/react-query";
import { CalendarClock, Plus, ServerOff, UserRoundCog } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useMemo } from "react";

import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";

import { Day } from "./account-cells";
import type { Actions } from "./account-columns";
import { LazyAgentAccountsGroup } from "./agent-accounts-lazy";
import { preloadLogin } from "./login-lazy";
import { isClaudeAccount, renewalDue, sharedVariables } from "./model";
import { ProviderCard } from "./provider-card";
import { type ProviderCardData, providerCards } from "./provider-cards";
import { catalogQuery, gatewayStateQuery, providerAccountsQuery } from "./provider-queries";
import { CLAUDE_TOKEN_VAR, type Account } from "./queries";

const code = (chunks: ReactNode) => <code className="rounded-xs bg-muted px-1 font-mono text-xs text-foreground">{chunks}</code>;

/** The accounts whose token ends within 30 days, soonest first, each with its day and a way to renew it. */
function RenewNotice({ accounts, now, onRenew }: { accounts: Account[]; now: number; onRenew: (account: Account) => void }) {
  const t = useTranslations("accounts.renewNotice");
  const due = accounts
    .flatMap((account) => {
      const when = isClaudeAccount(account) && account.state !== "expired" ? renewalDue(account, now) : null;
      return when ? [{ account, when }] : [];
    })
    .sort((a, b) => a.when.at.localeCompare(b.when.at));
  if (due.length === 0) return null;
  return (
    <section
      aria-labelledby="accounts-renew-title"
      className="flex items-start gap-2.5 rounded-md border border-attention/20 bg-attention-soft px-3 py-2.5 text-sm text-attention"
      data-testid="accounts-renew-notice"
    >
      <CalendarClock className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <div className="flex min-w-0 flex-col gap-1.5">
        <h2 id="accounts-renew-title" className="font-medium">
          {t("title", { count: due.length })}
        </h2>
        <ul className="flex flex-col gap-1">
          {due.map(({ account, when }) => (
            <li key={account.name} className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="font-mono text-[13px] [overflow-wrap:anywhere]">{account.name}</span>
              <span>{t.rich("ends", { day: () => <Day value={when.at} /> })}</span>
              <Button
                type="button"
                variant="link"
                size="sm"
                className="h-auto px-0 text-attention underline underline-offset-4"
                onClick={() => onRenew(account)}
                onPointerEnter={preloadLogin}
                onFocus={preloadLogin}
                aria-label={t("renewLabel", { name: account.name })}
                data-testid="accounts-renew-notice-renew"
              >
                {t("renew")}
              </Button>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

/** Said when the visitor has provider accounts and the hub runs no model gateway, or cannot reach it: none runs. */
export function GatewayNotice({ state }: { state: "off" | "error" }) {
  const t = useTranslations("accounts.provider.gateway");
  return (
    <section
      role="status"
      className="flex items-start gap-2.5 rounded-md border border-attention/20 bg-attention-soft px-3 py-2.5 text-sm text-attention"
      data-testid="accounts-gateway-notice"
      data-state={state}
    >
      <ServerOff className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <div className="flex min-w-0 flex-col gap-0.5">
        <h2 className="font-medium">{t(`${state}Title`)}</h2>
        <p className="text-pretty">{t(`${state}Text`)}</p>
      </div>
    </section>
  );
}

/**
 * The Providers tab: a reminder of the tokens that end soon, then one card per provider the visitor's model accounts
 * call (Claude, a key of Anthropic or OpenAI set as an env secret, each provider account of the catalog), then the
 * accounts of the agents of their projects, each marked as an agent's. Without a model account, the empty state, whose
 * button opens the catalog.
 */
export function ProvidersTab({
  accounts,
  now,
  onCatalog,
  onAdd,
  ...actions
}: { accounts: Account[]; now: number; onCatalog: () => void; onAdd: (card: ProviderCardData) => void } & Actions) {
  const t = useTranslations("accounts");
  const providers = useQuery(providerAccountsQuery(browserApi));
  const catalog = useQuery(catalogQuery(browserApi));
  const gateway = useQuery(gatewayStateQuery(browserApi));
  const models = useMemo(() => accounts.filter((account) => account.category === "model"), [accounts]);
  const shared = useMemo(() => sharedVariables(accounts), [accounts]);
  const cards = useMemo(
    () => providerCards(models, providers.data ?? [], catalog.data?.providers ?? []),
    [models, providers.data, catalog.data],
  );
  const viaGateway = models.some((account) => account.gateway);
  const waiting = viaGateway && (providers.isPending || catalog.isPending);
  const gatewayState = gateway.data === "off" || gateway.data === "error" ? gateway.data : null;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-4">
        <RenewNotice accounts={accounts} now={now} onRenew={actions.onRenew} />
        {viaGateway && gatewayState ? <GatewayNotice state={gatewayState} /> : null}
        {providers.isError && viaGateway ? (
          <p className="text-sm text-danger" role="alert" data-testid="accounts-providers-error">
            {t("provider.loadFailed")}
          </p>
        ) : null}
        {models.length === 0 ? (
          <EmptyState
            icon={UserRoundCog}
            title={t("empty.title")}
            description={t.rich("empty.description", { code, variable: CLAUDE_TOKEN_VAR })}
          >
            <Button onClick={onCatalog} data-testid="accounts-empty-add">
              <Plus aria-hidden="true" />
              {t("addProvider")}
            </Button>
          </EmptyState>
        ) : waiting ? (
          <TableSkeleton rows={3} />
        ) : (
          <div className="grid min-w-0 grid-cols-1 gap-4" data-testid="provider-cards">
            {cards.map((card) => (
              <ProviderCard key={card.id} card={card} shared={shared} now={now} onAdd={onAdd} {...actions} />
            ))}
          </div>
        )}
      </div>
      <LazyAgentAccountsGroup now={now} onlyIfAny={models.length === 0} />
    </div>
  );
}
