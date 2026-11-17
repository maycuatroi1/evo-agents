import type { Route } from "next";
import type { ReactNode } from "react";

import { type NavCount, type NavGroup, PLUGIN_AREAS, type ProjectNavItem } from "./nav";
import type { PluginNavLink } from "./plugin-nav";
import type { ShortcutId } from "./shortcuts";

/** A link of the sidebar as it draws it: an area of nav.ts, or a page a plugin adds. */
export type NavLink = {
  key: string;
  testId: string;
  name: string;
  icon: ReactNode;
  href: Route;
  active: boolean;
  shortcut?: ShortcutId;
  count?: NavCount;
  value: number | null;
  group?: NavGroup;
};

/**
 * The counts of a fold's items, one a kind (summed), for its row while the items are out of sight: closed, or in the
 * menu of the folded sidebar.
 */
export function foldCounts(links: readonly NavLink[]): { count: NavCount; value: number }[] {
  const sums = new Map<NavCount, number>();
  for (const link of links) {
    if (link.count !== undefined && link.value !== null && link.value > 0) {
      sums.set(link.count, (sums.get(link.count) ?? 0) + link.value);
    }
  }
  return [...sums].map(([count, value]) => ({ count, value }));
}

/**
 * The project's links for the sidebar: its own areas (`own`, PROJECT_NAV less those it hides, drawn as `links`) with
 * the pages its plugins add (`pages`) in the folds they name, after a fold's own items or before its item marked
 * `pluginsBefore`. The pages of a fold that shows none of its own items close the list, fold by fold.
 */
export function withPluginPages(own: readonly ProjectNavItem[], links: readonly NavLink[], pages: readonly PluginNavLink[]): NavLink[] {
  const waiting = new Map<NavGroup, NavLink[]>(PLUGIN_AREAS.map((group) => [group, []]));
  for (const page of pages) waiting.get(page.group)?.push({ ...page, value: null });
  const take = (group: NavGroup | undefined): NavLink[] => {
    if (!group) return [];
    const found = waiting.get(group) ?? [];
    waiting.delete(group);
    return found;
  };
  const listed = own.flatMap((item, index) => {
    const before = item.pluginsBefore ? take(item.group) : [];
    const last = item.group !== undefined && own[index + 1]?.group !== item.group;
    return [...before, links[index], ...(last ? take(item.group) : [])];
  });
  return [...listed, ...[...waiting.values()].flat()];
}
