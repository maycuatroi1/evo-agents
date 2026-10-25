import type { RunnableModel } from "@/lib/runnable-models";

import type { CatalogProvider, ProviderAccount } from "./provider-queries";
import { type Account, CLAUDE_TOKEN_VAR } from "./queries";

/**
 * The Providers and Models tabs without React: the visitor's model accounts by the provider they call, one card each,
 * and the models their projects may run, by provider. A Claude login, an Anthropic or OpenAI key set as an env secret
 * and a provider account of the catalog are all cards of the same shape: the provider, its keys, its models.
 */
export type ProviderCardData = {
  /** The catalog's id of the provider: claude, anthropic, openai, or the provider of a provider account. */
  id: string;
  entry: CatalogProvider | null;
  /** The model accounts that call it, env secrets and provider accounts alike, by name. */
  accounts: Account[];
  /** The provider accounts among them, as /v1/provider-accounts gives them, by name. */
  keys: Map<string, ProviderAccount>;
  /** The models its provider accounts serve, each once, in their order. */
  models: string[];
};

/** The card an account goes on: a provider account's catalog provider, else the provider its variable signs in to. */
export const cardOf = (account: Pick<Account, "gateway" | "provider">) => account.gateway?.provider ?? account.provider;

/**
 * The cards of the visitor's model accounts: Claude first, as runs sign in with it by default, then by the provider's
 * name. `catalog` names each provider and gives its colour; a provider it lacks keeps its id as its name.
 */
export function providerCards(
  accounts: readonly Account[],
  providers: readonly ProviderAccount[],
  catalog: readonly CatalogProvider[],
): ProviderCardData[] {
  const entries = new Map(catalog.map((entry) => [entry.id, entry]));
  const byName = new Map(providers.map((provider) => [provider.name, provider]));
  const cards = new Map<string, ProviderCardData>();
  for (const account of accounts) {
    if (account.category !== "model") continue;
    const id = cardOf(account);
    const card: ProviderCardData = cards.get(id) ?? { id, entry: entries.get(id) ?? null, accounts: [], keys: new Map(), models: [] };
    card.accounts.push(account);
    const key = account.gateway ? byName.get(account.name) : undefined;
    if (key) {
      card.keys.set(key.name, key);
      for (const model of key.models) if (!card.models.includes(model)) card.models.push(model);
    }
    cards.set(id, card);
  }
  const rank = (card: ProviderCardData) => (card.id === "claude" ? 0 : 1);
  return [...cards.values()].sort((a, b) => rank(a) - rank(b) || cardName(a).localeCompare(cardName(b)));
}

export const cardName = (card: Pick<ProviderCardData, "id" | "entry">) => card.entry?.name ?? card.id;

/** Whether the visitor signs in to Claude on their workers: a Claude account, whose runs take Claude Code's models. */
export const hasClaudeLogin = (accounts: readonly Account[]) => accounts.some((account) => account.env_var === CLAUDE_TOKEN_VAR);

/** One model of the Models tab: what a run asks for, who serves it, the runtimes that run it and where. */
export type ModelRow = RunnableModel & { projects: string[] };

/** The runnable models of every project, merged by name: a model two projects may run is one row naming both. */
export function mergeRunnable(lists: readonly { project: string; models: readonly RunnableModel[] }[]): ModelRow[] {
  const rows = new Map<string, ModelRow>();
  for (const { project, models } of lists) {
    for (const model of models) {
      const row = rows.get(model.model);
      if (!row) {
        rows.set(model.model, { ...model, runtimes: [...model.runtimes], accounts: [...model.accounts], projects: [project] });
        continue;
      }
      row.projects.push(project);
      for (const runtime of model.runtimes) if (!row.runtimes.includes(runtime)) row.runtimes.push(runtime);
      for (const account of model.accounts) if (!row.accounts.includes(account)) row.accounts.push(account);
    }
  }
  return [...rows.values()].sort((a, b) => a.model.localeCompare(b.model));
}

/** The text colour that reads on a catalog colour: white or black, whichever has the higher WCAG contrast with it. */
export function inkOn(color: string): "light" | "dark" {
  const hex = /^#([0-9a-f]{6})$/i.exec(color)?.[1];
  if (!hex) return "light";
  const [r, g, b] = [0, 2, 4].map((at) => {
    const channel = Number.parseInt(hex.slice(at, at + 2), 16) / 255;
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  const luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  // Contrast with white is 1.05 / (L + 0.05), with black (L + 0.05) / 0.05; the higher of the two is never below 4.58:1.
  return (luminance + 0.05) / 0.05 > 1.05 / (luminance + 0.05) ? "dark" : "light";
}
