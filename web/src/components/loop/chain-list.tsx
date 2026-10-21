"use client";

import { CornerDownRight } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { Identifier, NAME_LINK } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { goalsHref, itemHref, type LoopItem } from "@/lib/loop-queries";
import { cn } from "@/lib/utils";

import { ItemStateBadge, KindIcon, useItemTitle, useWaitingText } from "./badges";
import { type Chain, chainsOf } from "./model";

/** One work item in its chain's list: indented under the one it came of, its title opening its chain's page. */
export function ChainItemRow({ project, item, depth, current = false }: { project: string; item: LoopItem; depth: number; current?: boolean }) {
  const title = useItemTitle();
  const waiting = useWaitingText();
  const why = waiting(item);
  return (
    <li
      className={cn("flex min-h-11 items-center gap-3 px-4 py-2", current && "bg-surface-selected")}
      style={{ paddingLeft: `${16 + Math.min(depth, 4) * 20}px` }}
      data-testid="chain-item"
      data-item-id={item.id}
      data-depth={depth}
      aria-current={current ? "true" : undefined}
    >
      {depth > 0 ? <CornerDownRight className="size-4 shrink-0 text-fg-subtle" aria-hidden="true" /> : null}
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <KindIcon kind={item.kind} />
          <Link href={itemHref(project, item.id)} className={cn(NAME_LINK, "min-w-0 truncate text-sm")} data-testid="chain-item-link">
            {title(item)}
          </Link>
          <span className="font-mono text-xs text-fg-subtle tabular-nums">#{item.id}</span>
        </div>
        {why ? (
          <span className="truncate text-xs text-fg-subtle" title={why}>
            {why}
          </span>
        ) : null}
      </div>
      <span className="hidden text-xs text-fg-subtle sm:inline">
        <When value={item.updated_at} short />
      </span>
      <ItemStateBadge state={item.state} />
    </li>
  );
}

function ChainCard({ project, chain }: { project: string; chain: Chain }) {
  const t = useTranslations("loop.chains");
  const title = useItemTitle();
  const [first] = chain.rows;
  const goal = chain.rows.find((row) => row.item.goal)?.item.goal ?? null;
  return (
    <section aria-label={t("label", { title: title(first.item) })} className="min-w-0" data-testid="loop-chain" data-root={chain.root}>
      <div className="flex min-h-10 flex-wrap items-center gap-2 border-b bg-surface-sunken px-4 py-2">
        <h3 className="min-w-0 truncate text-[13px] leading-[18px] font-semibold">{t("title", { title: title(first.item) })}</h3>
        {goal ? <Identifier value={goal} href={goalsHref(project, goal)} /> : null}
        <span className="ml-auto text-xs text-fg-subtle tabular-nums">{t("count", { count: chain.rows.length })}</span>
      </div>
      <ol className="divide-y">
        {chain.rows.map((row) => (
          <ChainItemRow key={row.item.id} project={project} item={row.item} depth={row.depth} />
        ))}
      </ol>
    </section>
  );
}

/**
 * The listed work items by chain: each chain from the first work item to the last that came of it, the chain changed
 * last first. A chain whose first work item is not listed (filtered out, or older than the list) starts from the
 * first one listed.
 */
export function ChainList({ project, items }: { project: string; items: LoopItem[] }) {
  const t = useTranslations("loop.items");
  const chains = chainsOf(items);
  if (chains.length === 0) return <p className="px-4 py-6 text-center text-[13px] text-muted-foreground">{t("none")}</p>;
  return (
    <div className="flex flex-col divide-y" data-testid="loop-chains">
      {chains.map((chain) => (
        <ChainCard key={chain.root} project={project} chain={chain} />
      ))}
    </div>
  );
}
