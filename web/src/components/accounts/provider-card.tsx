"use client";

import { Plus } from "lucide-react";
import { useTranslations } from "next-intl";

import { DataCard } from "@/components/data/data-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

import type { Actions } from "./account-columns";
import { preloadLogin } from "./login-lazy";
import { ModelTable } from "./model-table";
import { cardName, type ProviderCardData } from "./provider-cards";
import { ProviderMark } from "./provider-mark";
import { hostOf } from "./provider-model";

/** How many model names a card shows before "and N more". */
const MODELS_SHOWN = 12;

/**
 * One provider of the Providers tab: its mark, its name, the host of its base URL and its API format, how many keys
 * it has, then its keys as a table (each with its state, weight and priority, runs, cost, holder and actions; a Claude
 * account with its usage bars and Renew token), then the models its keys serve. Claude is a card like any other: it
 * signs in on the worker, so it has no host, and its runs take Claude Code's models.
 */
export function ProviderCard({
  card,
  shared,
  now,
  onAdd,
  ...actions
}: { card: ProviderCardData; shared: Set<string>; now: number; onAdd: (card: ProviderCardData) => void } & Actions) {
  const t = useTranslations("accounts.provider.card");
  const name = cardName(card);
  const titleId = `provider-card-${card.id}`;
  const first = card.accounts[0];
  const key = card.keys.values().next().value;
  const host = key ? hostOf(key.base_url) : (first?.gateway?.base_url_host ?? null);
  const format = key?.api_format ?? first?.gateway?.api_format ?? null;
  const login = card.id === "claude";
  const shown = card.models.slice(0, MODELS_SHOWN);
  const more = card.models.length - shown.length;
  return (
    <section aria-labelledby={titleId} data-testid="provider-card" data-provider-id={card.id} className="min-w-0">
      <DataCard
        toolbar={
          <header className="flex flex-wrap items-start gap-3 border-b px-4 py-3">
            <ProviderMark id={card.id} entry={card.entry} />
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <div className="flex flex-wrap items-center gap-2">
                <h3 id={titleId} className="text-[15px] leading-[22px] font-semibold [overflow-wrap:anywhere]">
                  {name}
                </h3>
                <Badge variant="secondary" data-testid="provider-card-count">
                  {t("keys", { count: card.accounts.length })}
                </Badge>
              </div>
              <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-fg-muted">
                {login ? (
                  <span data-testid="provider-card-host">{t("login")}</span>
                ) : host ? (
                  <span className="font-mono [overflow-wrap:anywhere]" data-testid="provider-card-host">
                    {host}
                  </span>
                ) : (
                  <span data-testid="provider-card-host">{t("envKey")}</span>
                )}
                {format ? (
                  <span data-testid="provider-card-format">
                    <span className="sr-only">{t("format")}: </span>
                    <Badge variant="outline" className="font-mono">
                      {format}
                    </Badge>
                  </span>
                ) : null}
              </p>
            </div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => onAdd(card)}
              onPointerEnter={login ? preloadLogin : undefined}
              onFocus={login ? preloadLogin : undefined}
              aria-label={t("addLabel", { name })}
              data-testid="provider-card-add"
            >
              <Plus aria-hidden="true" />
              {t("add")}
            </Button>
          </header>
        }
        footer={
          card.models.length > 0 ? (
            <div className="flex flex-col gap-1.5 py-1" data-testid="provider-card-models">
              <h4 className="text-xs font-medium text-fg-muted">{t("models", { count: card.models.length })}</h4>
              <ul className="flex flex-wrap gap-1.5">
                {shown.map((model) => (
                  <li key={model}>
                    <Badge variant="outline" className="max-w-full font-mono [overflow-wrap:anywhere] whitespace-normal" data-testid="provider-card-model">
                      {model}
                    </Badge>
                  </li>
                ))}
                {more > 0 ? <li className="text-xs text-fg-subtle">{t("moreModels", { count: more })}</li> : null}
              </ul>
            </div>
          ) : login ? (
            <p className="py-1 text-xs text-fg-muted" data-testid="provider-card-models">
              {t("loginModels")}
            </p>
          ) : null
        }
      >
        <ModelTable accounts={card.accounts} shared={shared} now={now} caption={t("caption", { name })} {...actions} />
      </DataCard>
    </section>
  );
}
