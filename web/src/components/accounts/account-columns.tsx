"use client";

import { Pause, Pencil, Play, RefreshCw } from "lucide-react";
import type { Route } from "next";
import { useFormatter, useTranslations } from "next-intl";

import { CellMain } from "@/components/data/data-table";
import { RunRef } from "@/components/data/identifier";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

import { At } from "./account-cells";
import { preloadLogin } from "./login-lazy";
import { isClaudeAccount } from "./model";
import { ProviderLogo } from "./provider-logo";
import type { Account } from "./queries";

/** The cells the model and the service tables of the Accounts page have in common. */

/** Columns that give way at narrow widths, by column id. */
export const NARROW_HIDDEN = {
  projects: "hidden lg:table-cell",
  workers: "hidden xl:table-cell",
  runs: "hidden lg:table-cell",
  cost: "hidden lg:table-cell",
  lastUsed: "hidden md:table-cell",
  heldBy: "hidden sm:table-cell",
};

export type Actions = {
  onEdit: (account: Account) => void;
  onPause: (account: Account) => void;
  onRenew: (account: Account) => void;
};

/**
 * The first cell: the provider's logo, the account's name, then its provider's name as text, its variable, and
 * "weight N, priority M" only when `ranked` (the visitor has two or more accounts for the variable).
 */
export function AccountCell({ account, ranked }: { account: Account; ranked: boolean }) {
  const t = useTranslations("accounts");
  const sub = [t(`providers.${account.provider}`), account.env_var, ranked ? t("rank", { weight: account.weight, priority: account.priority }) : null]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="flex min-w-0 items-center gap-2.5">
      <ProviderLogo provider={account.provider} />
      <CellMain sub={sub} subTitle={sub} subTestId="account-target">
        <span className="truncate font-mono text-[13px]" title={account.name} data-account-name={account.name}>
          {account.name}
        </span>
      </CellMain>
    </div>
  );
}

export function RunsCell({ day, week }: { day: number; week: number }) {
  const t = useTranslations("accounts");
  return (
    <span title={t("runsTitle", { day, week })} data-testid="account-runs">
      {day} / {week}
    </span>
  );
}

/**
 * What a model account's runs cost over 24 hours and 7 days: "Cost" for an API key, and "API value" for a Claude
 * subscription, whose runs are not billed per run, with the tooltip that says so.
 */
export function CostCell({ account }: { account: Account }) {
  const t = useTranslations("accounts");
  const format = useFormatter();
  if (account.cost_24h_usd === null || account.cost_7d_usd === null) return <span className="text-fg-subtle">-</span>;
  const usd = (value: number) => format.number(value, { style: "currency", currency: "USD" });
  const day = usd(account.cost_24h_usd);
  const week = usd(account.cost_7d_usd);
  const subscription = account.provider === "claude";
  return (
    <span className="inline-flex flex-col items-end">
      <span title={t("costTitle", { day, week })} data-testid="account-cost">
        {day} / {week}
      </span>
      {subscription ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <button type="button" className="cursor-help rounded-sm text-xs text-fg-subtle underline decoration-dotted underline-offset-2" data-testid="account-cost-label">
              {t("apiValue")}
            </button>
          </TooltipTrigger>
          <TooltipContent>{t("apiValueHint")}</TooltipContent>
        </Tooltip>
      ) : (
        <span className="text-xs text-fg-subtle" data-testid="account-cost-label">
          {t("costLabel")}
        </span>
      )}
    </span>
  );
}

/** The runs holding the account now; nothing at all when there is none. */
export function HeldByCell({ held }: { held: Account["held_by"] }) {
  const t = useTranslations("accounts");
  if (held.length === 0) return null;
  return (
    <span className="inline-flex flex-wrap gap-x-2" data-testid="account-held-by">
      {held.map((run) => (
        <RunRef
          key={run.run_id}
          id={run.run_id}
          href={`/p/${encodeURIComponent(run.project)}/runs/${run.run_id}` as Route}
          label={t("heldLabel", { id: run.run_id, project: run.project })}
          data-run-id={run.run_id}
        />
      ))}
    </span>
  );
}

/** When the account was last leased to a run; a dash for one that never was. */
export function LastUsedCell({ at }: { at: string | null }) {
  return at ? <At value={at} /> : <span className="text-fg-subtle">-</span>;
}

/** Renew token (a Claude account's), Edit and Pause or Resume of one account. */
export function ActionButtons({ account, onEdit, onPause, onRenew }: { account: Account } & Actions) {
  const t = useTranslations("accounts");
  const paused = account.paused_at !== null;
  return (
    <>
      {isClaudeAccount(account) ? (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          aria-label={t("renewLabel", { name: account.name })}
          onClick={() => onRenew(account)}
          onPointerEnter={preloadLogin}
          onFocus={preloadLogin}
          data-testid="account-renew"
        >
          <RefreshCw aria-hidden="true" />
          <span className="hidden sm:inline">{t("renew")}</span>
        </Button>
      ) : null}
      <Button
        type="button"
        variant="ghost"
        size="sm"
        aria-label={t("editLabel", { name: account.name })}
        onClick={() => onEdit(account)}
        data-testid="account-edit"
      >
        <Pencil aria-hidden="true" />
        <span className="hidden sm:inline">{t("edit")}</span>
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        aria-label={t(paused ? "resumeLabel" : "pauseLabel", { name: account.name })}
        onClick={() => onPause(account)}
        data-testid={paused ? "account-resume" : "account-pause"}
      >
        {paused ? <Play aria-hidden="true" /> : <Pause aria-hidden="true" />}
        <span className="hidden sm:inline">{t(paused ? "resume" : "pause")}</span>
      </Button>
    </>
  );
}
