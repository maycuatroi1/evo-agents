import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Bandage,
  BookOpen,
  BookMarked,
  Bot,
  Brain,
  Building2,
  ChartColumn,
  FolderKanban,
  GitMerge,
  Globe,
  Goal,
  Inbox,
  IterationCw,
  LayoutDashboard,
  LayoutGrid,
  ListChecks,
  LockKeyhole,
  type LucideIcon,
  MessagesSquare,
  MonitorPlay,
  MoonStar,
  Network,
  Puzzle,
  RadioTower,
  Server,
  Settings,
  Shield,
  Ship,
  Signpost,
  Sparkles,
  Tags,
  Telescope,
  TrendingUp,
  UserRoundCog,
} from "lucide-react";
import type { Route } from "next";

import { browserApi } from "@/lib/api/browser";
import { type FeatureId, featureOn, featuresQuery } from "@/lib/hub-settings";

/**
 * The sidebar's links, in the kit's AppShell order: Home, Inbox, Monitor and Chats; the current project's areas, then
 * the pages its enabled plugins add (shell/plugin-nav.tsx) before its Plugins; the hub-wide areas (org chart, workers,
 * memories, skills, then secrets, accounts and administration). A step that adds a project area adds one entry to
 * PROJECT_NAV, a hub-wide area one entry to HUB_NAV, and one label under `nav` in messages/vi.json and
 * messages/en.json. Icons follow the Iconography table of web/DESIGN.md. An item that names a `feature` leaves the
 * sidebar and the palette while a hub admin has that feature off, for the hub or for the project shown.
 *
 * An item that names a `group` sits in that fold of the sidebar (NAV_GROUPS) instead of on its own row, so the
 * sidebar keeps to the places a visitor opens every day: the fold opens on a click, by itself when the page shown is
 * one of its items, and stays as the visitor left it (the cookie NAV_GROUPS_COOKIE). An item with a count never sits
 * in a group, so a closed fold never hides a number. The items of a group follow each other in their list.
 */
export type NavLabel =
  | "home"
  | "inbox"
  | "monitor"
  | "overview"
  | "decide"
  | "plans"
  | "runs"
  | "mergeQueue"
  | "insights"
  | "signals"
  | "problems"
  | "opportunities"
  | "goals"
  | "loop"
  | "curator"
  | "releases"
  | "browser"
  | "agents"
  | "chats"
  | "org"
  | "memories"
  | "skills"
  | "kg"
  | "plugins"
  | "workers"
  | "myMemories"
  | "globalSkills"
  | "secrets"
  | "accounts"
  | "admin";

/**
 * A number at the end of a nav item, in its tone: the decisions waiting for the visitor's answer (attention), the
 * project's active runs (running), the runs at work in every project of the visitor's grants (running), the messages
 * of others in the visitor's chats that they have not read (attention). Hidden while it is zero or unknown.
 */
export type NavCount = "openDecisions" | "activeRuns" | "runningRuns" | "unreadChats";

/** A fold of the sidebar, its label under `nav.groups` in the message files. */
export type NavGroup = "delivery" | "improvement" | "knowledge" | "apps" | "settings";

/** Each fold's icon, before its label; the fold's items keep their own. */
export const NAV_GROUPS: Record<NavGroup, LucideIcon> = {
  delivery: Ship,
  improvement: TrendingUp,
  knowledge: BookMarked,
  apps: LayoutGrid,
  settings: Settings,
};

/** The cookie that keeps which folds are open, read on the server so the sidebar renders as the visitor left it. */
export const NAV_GROUPS_COOKIE = "hub_nav_groups";

export type ProjectNavItem = {
  label: NavLabel;
  icon: LucideIcon;
  /** Path below /p/{project}; "" is the project's overview. */
  segment: string;
  count?: NavCount;
  /** The feature a hub admin may turn off for the hub or the project, which hides the item (lib/hub-settings.ts). */
  feature?: FeatureId;
  /** The fold the item sits in; none for a row of its own. */
  group?: NavGroup;
};

export type HubNavItem = {
  label: NavLabel;
  icon: LucideIcon;
  href: Route;
  adminOnly?: boolean;
  count?: NavCount;
  /** The feature a hub admin may turn off for the hub, which hides the item. */
  feature?: FeatureId;
  /** The fold the item sits in; none for a row of its own. */
  group?: NavGroup;
};

/**
 * The first group, without a label: where the visitor starts, what waits for them (decisions, chats) and the runs they
 * watch live.
 */
export const HOME_NAV: readonly HubNavItem[] = [
  { label: "home", icon: LayoutDashboard, href: "/" },
  { label: "inbox", icon: Inbox, href: "/inbox", count: "openDecisions" },
  { label: "monitor", icon: MonitorPlay, href: "/monitor", count: "runningRuns", feature: "monitor" },
  { label: "chats", icon: MessagesSquare, href: "/chats", count: "unreadChats", feature: "chats" },
];

export const PROJECT_NAV: readonly ProjectNavItem[] = [
  { label: "overview", icon: FolderKanban, segment: "" },
  { label: "decide", icon: Signpost, segment: "decide" },
  { label: "plans", icon: ListChecks, segment: "plans" },
  { label: "runs", icon: Activity, segment: "runs", count: "activeRuns" },
  { label: "mergeQueue", icon: GitMerge, segment: "merge-queue", group: "delivery" },
  { label: "releases", icon: Tags, segment: "releases", group: "delivery" },
  { label: "problems", icon: Bandage, segment: "problems", group: "improvement" },
  { label: "opportunities", icon: Telescope, segment: "opportunities", group: "improvement" },
  { label: "goals", icon: Goal, segment: "goals", group: "improvement" },
  { label: "signals", icon: RadioTower, segment: "signals", group: "improvement" },
  { label: "loop", icon: IterationCw, segment: "loop", group: "improvement" },
  { label: "insights", icon: ChartColumn, segment: "insights", group: "improvement" },
  { label: "curator", icon: MoonStar, segment: "curator", feature: "curator", group: "improvement" },
  { label: "agents", icon: Bot, segment: "agents", group: "knowledge" },
  { label: "memories", icon: BookOpen, segment: "memories", group: "knowledge" },
  { label: "skills", icon: Sparkles, segment: "skills", group: "knowledge" },
  { label: "kg", icon: Network, segment: "kg", feature: "kg", group: "knowledge" },
  { label: "browser", icon: Globe, segment: "browser", group: "knowledge" },
  { label: "plugins", icon: Puzzle, segment: "plugins", group: "apps" },
];

export const HUB_NAV: readonly HubNavItem[] = [
  { label: "workers", icon: Server, href: "/workers" },
  { label: "org", icon: Building2, href: "/org" },
  { label: "myMemories", icon: Brain, href: "/memories" },
  { label: "globalSkills", icon: Sparkles, href: "/skills" },
  { label: "secrets", icon: LockKeyhole, href: "/secrets", group: "settings" },
  { label: "accounts", icon: UserRoundCog, href: "/accounts", group: "settings" },
  { label: "admin", icon: Shield, href: "/admin", adminOnly: true, group: "settings" },
];

/**
 * One row of a sidebar section: an item on its own, or a fold with its items, at the place of its first item. A fold
 * left with a single item shows that item on its own row: a fold of one hides it for nothing.
 */
export type NavRow<T> = { kind: "item"; item: T } | { kind: "group"; group: NavGroup; items: T[] };

export function navRows<T extends { group?: NavGroup }>(items: readonly T[]): NavRow<T>[] {
  const rows: NavRow<T>[] = [];
  const held = new Map<NavGroup, T[]>();
  for (const item of items) {
    if (!item.group) {
      rows.push({ kind: "item", item });
      continue;
    }
    const group = held.get(item.group);
    if (group) {
      group.push(item);
      continue;
    }
    const fresh = [item];
    held.set(item.group, fresh);
    rows.push({ kind: "group", group: item.group, items: fresh });
  }
  return rows.map((row) => (row.kind === "group" && row.items.length === 1 ? { kind: "item", item: row.items[0] } : row));
}

/** The open folds a cookie value names, leaving out any this web does not know; none from a value it cannot read. */
export function parseNavGroups(value: string | undefined): NavGroup[] {
  if (!value) return [];
  let names: string[];
  try {
    names = decodeURIComponent(value).split(",");
  } catch {
    return [];
  }
  const known = new Set(Object.keys(NAV_GROUPS));
  return [...new Set(names)].filter((name): name is NavGroup => known.has(name));
}

/** Keeps the open folds for a year, for every page of the hub. */
export function writeNavGroups(open: Iterable<NavGroup>): void {
  const value = encodeURIComponent([...open].sort().join(","));
  document.cookie = `${NAV_GROUPS_COOKIE}=${value}; path=/; max-age=31536000; samesite=lax`;
}

export function projectHref(project: string, segment = ""): Route {
  const base = `/p/${encodeURIComponent(project)}`;
  return (segment ? `${base}/${segment}` : base) as Route;
}

/** Whether `href` is the page shown, or (for a section) one of its pages. */
export function isActive(pathname: string, href: string, exact: boolean): boolean {
  if (exact || href === "/") return pathname === href;
  return pathname === href || pathname.startsWith(`${href}/`);
}

/**
 * Whether a nav item shows, for the sidebar and the palette: unless it names a feature a hub admin turned off, for the
 * hub or for `project` (GET /v1/features, which the shell reads on the server). Every item shows until it answers.
 */
export function useNavShown(): (item: { feature?: FeatureId }, project?: string | null) => boolean {
  const { data: features } = useQuery(featuresQuery(browserApi));
  return (item, project) => featureOn(features, item.feature, project);
}

/** A count as a nav item shows it: up to 99, then 99+. */
export function countText(count: number): string {
  return count > 99 ? "99+" : String(count);
}
