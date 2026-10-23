"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, KeyRound, type LucideIcon, Megaphone, PiggyBank, PowerOff } from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { pluginOn } from "@/lib/plugin-pages";
import { projectPluginsQuery } from "@/lib/plugin-queries";

import { budgetSpent, MARKETING_PLUGIN, type MarketingChannel, type MarketingStatus, marketingChannelsQuery, marketingHref, marketingStatusQuery } from "./api";
import { useMoney } from "./chrome";

type Kind = "breaker" | "session" | "budget";
export type GrowthItem = { kind: Kind; key: string; title: string };

const ICONS: Record<Kind, LucideIcon> = { breaker: PowerOff, session: KeyRound, budget: PiggyBank };

/**
 * What waits for a person in the project's growth: each channel whose breaker is open (only an admin closes it), each
 * browser channel whose login session expired (its owner logs in again), and the month's budget once it is spent.
 */
export function growthItems(
  channels: MarketingChannel[],
  status: MarketingStatus,
  words: { breaker: (channel: string, account: string) => string; session: (channel: string, account: string) => string; budget: (month: string) => string },
): GrowthItem[] {
  const items: GrowthItem[] = [];
  for (const channel of channels) {
    if (channel.paused) items.push({ kind: "breaker", key: `breaker-${channel.id}`, title: words.breaker(channel.channel, channel.account) });
  }
  for (const channel of channels) {
    if (channel.adapter === "browser" && channel.session_state === "stale") {
      items.push({ kind: "session", key: `session-${channel.id}`, title: words.session(channel.channel, channel.account) });
    }
  }
  if (budgetSpent(status.budget)) items.push({ kind: "budget", key: "budget", title: words.budget(status.budget.month) });
  return items;
}

function Items({ project }: { project: string }) {
  const t = useTranslations("marketing.decide");
  const money = useMoney();
  const channels = useQuery(marketingChannelsQuery(browserApi, project));
  const status = useQuery(marketingStatusQuery(browserApi, project));
  if (!channels.data || !status.data) return null;
  const budget = status.data.budget;
  const items = growthItems(channels.data, status.data, {
    breaker: (channel, account) => t("breaker", { channel, account }),
    session: (channel, account) => t("session", { channel, account }),
    budget: (month) => t("budget", { month, cap: money(budget.budget_usd) }),
  });
  if (items.length === 0) return null;
  return (
    <section aria-labelledby="decide-growth-title" className="min-w-0 rounded-md border bg-card shadow-raised" data-testid="decide-growth">
      <div className="flex min-h-12 flex-wrap items-center gap-x-2 gap-y-1 border-b px-4 py-2">
        <Megaphone className="size-4 text-muted-foreground" aria-hidden="true" />
        <h2 id="decide-growth-title" className="text-[15px] leading-[22px] font-semibold">
          {t("title")}
        </h2>
        <span className="text-xs text-fg-subtle">{t("note")}</span>
        <span className="ml-auto inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-attention-soft px-1.5 text-xs font-medium text-attention tabular-nums">
          <span aria-hidden="true">{items.length}</span>
          <span className="sr-only">{t("waiting", { count: items.length })}</span>
        </span>
      </div>
      <ul className="divide-y">
        {items.map((item) => {
          const Icon = ICONS[item.kind];
          return (
            <li key={item.key} className="flex flex-wrap items-start gap-x-3 gap-y-2 px-4 py-3" data-testid="growth-item" data-kind={item.kind}>
              <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-sm bg-surface-sunken text-muted-foreground" aria-hidden="true">
                <Icon className="size-4" />
              </span>
              <div className="flex min-w-0 flex-1 basis-64 flex-col gap-1">
                <Tag>{t(`kind.${item.kind}`)}</Tag>
                <p className="text-sm text-foreground [overflow-wrap:anywhere]">{item.title}</p>
                <p className="text-xs text-fg-subtle">{t(`who.${item.kind}`)}</p>
              </div>
              <Button asChild variant="secondary" size="sm" className="max-sm:ml-10">
                <a href={marketingHref(project)} aria-label={t("openLabel", { title: item.title })} data-testid="growth-item-open">
                  <ArrowUpRight aria-hidden="true" />
                  {t("open")}
                </a>
              </Button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/**
 * The growth decisions on a project's Decisions page, once the project enabled the plugin marketing: nothing at all in a
 * project without it, nor while nothing waits.
 */
export function GrowthDecisions({ project }: { project: string }) {
  const plugins = useQuery(projectPluginsQuery(browserApi, project));
  const marketing = plugins.data?.find((plugin) => plugin.name === MARKETING_PLUGIN);
  if (!marketing || !pluginOn(marketing)) return null;
  return <Items project={project} />;
}
