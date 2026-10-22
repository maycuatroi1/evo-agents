"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { Identifier, NAME_LINK } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { goalsHref, itemHref, type LoopItem } from "@/lib/loop-queries";
import { cn } from "@/lib/utils";

import { ItemStateBadge, KindIcon, useItemTitle, useWaitingText } from "./badges";

const NARROW_HIDDEN = { goal: "hidden sm:table-cell", attempts: "hidden lg:table-cell", updated: "hidden md:table-cell" };

/** A work item's tries: "2 runs, 1 failed", or nothing before its first run. */
export function useTries(): (item: Pick<LoopItem, "attempts" | "failures">) => string | null {
  const t = useTranslations("loop.items");
  return (item) => (item.attempts === 0 ? null : t("tries", { attempts: item.attempts, failures: item.failures }));
}

/**
 * Work items as one table: each opens its chain, with its kind, why it waits (or why it ended), its goal, its state,
 * its tries and its last change. Under 768 px each is a row of a list: its title, its state, why it waits. `items`
 * come in the order to show them; the table sorts by a column only when asked.
 */
export function ItemsTable({
  project,
  items,
  caption,
  withGoal = true,
  testId = "loop-items-table",
}: {
  project: string;
  items: LoopItem[];
  caption: string;
  /** The goal column, left out on a goal's own page. */
  withGoal?: boolean;
  testId?: string;
}) {
  const t = useTranslations("loop.items");
  const title = useItemTitle();
  const waiting = useWaitingText();
  const tries = useTries();
  const columns = useMemo(() => {
    const helper = dataTableColumns<LoopItem>();
    return helper.columns([
      helper.accessor("id", {
        header: () => t("columns.item"),
        meta: { primary: true },
        enableSorting: false,
        cell: (info) => {
          const item = info.row.original;
          const why = waiting(item);
          return (
            <CellMain sub={why ?? undefined} subTitle={why ?? undefined} subTestId="item-waiting">
              <KindIcon kind={item.kind} />
              <Link href={itemHref(project, item.id)} className={cn(NAME_LINK, "truncate")} data-testid="item-link" data-item-id={item.id}>
                {title(item)}
              </Link>
              <span className="shrink-0 font-mono text-xs text-fg-subtle tabular-nums">#{item.id}</span>
            </CellMain>
          );
        },
      }),
      helper.accessor("goal", {
        header: () => t("columns.goal"),
        enableSorting: false,
        cell: (info) => {
          const goal = info.getValue();
          return goal ? <Identifier value={goal} href={goalsHref(project, goal)} testId="item-goal" /> : <span className="text-fg-subtle">-</span>;
        },
      }),
      helper.accessor("state", {
        header: () => t("columns.state"),
        enableSorting: false,
        cell: (info) => <ItemStateBadge state={info.getValue()} />,
      }),
      helper.display({
        id: "attempts",
        header: () => t("columns.tries"),
        meta: { numeric: true },
        cell: (info) => <span className="whitespace-nowrap">{tries(info.row.original) ?? "-"}</span>,
      }),
      helper.accessor("updated_at", {
        id: "updated",
        header: () => t("columns.updated"),
        meta: { numeric: true },
        sortFn: "datetime",
        cell: (info) => <When value={info.getValue()} short />,
      }),
    ]);
  }, [t, project, title, waiting, tries]);
  return (
    <DataTable
      data={items}
      columns={columns}
      caption={caption}
      getRowId={(item) => String(item.id)}
      columnClassNames={withGoal ? NARROW_HIDDEN : { ...NARROW_HIDDEN, goal: "hidden" }}
      empty={t("none")}
      testId={testId}
      mobile={(item) => ({
        title: title(item),
        titleText: title(item),
        href: itemHref(project, item.id),
        status: <ItemStateBadge state={item.state} />,
        meta: [waiting(item), withGoal ? item.goal : null, `#${item.id}`].filter(Boolean).join(", "),
        data: { "item-id": item.id },
      })}
    />
  );
}
