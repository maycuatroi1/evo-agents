import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlarmClock,
  Bandage,
  BookOpen,
  BookMarked,
  Bot,
  Brain,
  Building2,
  ChartColumn,
  Compass,
  FolderKanban,
  GitMerge,
  Globe,
  Goal,
  Hammer,
  Inbox,
  IterationCw,
  LayoutDashboard,
  LayoutGrid,
  ListChecks,
  LockKeyhole,
  type LucideIcon,
  MessageCircle,
  MessagesSquare,
  MoonStar,
  Network,
  Puzzle,
  RadioTower,
  Server,
  Settings,
  Shield,
  Signpost,
  Sparkles,
  Store,
  Tags,
  Telescope,
  TrendingUp,
  UserRoundCog,
  Workflow,
} from "lucide-react";
import type { Route } from "next";

import { browserApi } from "@/lib/api/browser";
import { type FeatureId, featureOn, featuresQuery } from "@/lib/hub-settings";

/**
 * The sidebar's links, in the kit's AppShell order: Home (the chat home), Today, Inbox and Chats; the current
 * project's areas, then the pages its enabled plugins add (shell/plugin-nav.tsx) before its Plugins; the hub-wide
 * areas (org chart, workers, memories, skills, then secrets, accounts and administration). A step that adds a project
 * area adds one entry to PROJECT_NAV, a hub-wide area one entry to HUB_NAV, and one label under `nav` in
 * messages/vi.json and messages/en.json. Icons follow the Iconography table of web/DESIGN.md. An item that names a
 * `feature` leaves the sidebar and the palette while a hub admin has that feature off, for the hub or for the project
 * shown.
 *
 * A project area marked `top` sits in the first group, after Home, Today, Inbox and Chats, for the project shown: its
 * agent systems and its knowledge graph, the core of a project, one click from anywhere. The palette and the breadcrumb
 * still name each as the project's area it is.
 *
 * An item that names a `group` sits in that fold of the sidebar (NAV_GROUPS) instead of on its own row, so the
 * sidebar keeps to the places a visitor opens every day: the fold opens on a click, by itself when the page shown is
 * one of its items, and stays as the visitor left it (the cookie NAV_GROUPS_COOKIE). The project's two cycles come
 * first, open until the visitor closes them (NAV_GROUPS_OPEN): the Business cycle, what to deliver, to whom and why
 * (signals, problems, opportunities, goals), and the Development cycle, how it gets built (plans, runs, the Loop and
 * what ships it). A closed fold shows the counts of its items on its row, so it never hides a number. The items of a
 * group follow each other in their list. A plugin's page sits in the fold its manifest names (`area`, PLUGIN_AREAS),
 * after the fold's own items or before the item marked `pluginsBefore`; one that names none sits in Apps.
 */
export type NavLabel =
  | "home"
  | "today"
  | "inbox"
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
  | "cronJobs"
  | "browser"
  | "agents"
  | "systems"
  | "chats"
  | "org"
  | "memories"
  | "skills"
  | "kg"
  | "plugins"
  | "workers"
  | "myMemories"
  | "globalSkills"
  | "market"
  | "secrets"
  | "accounts"
  | "admin";

/**
 * A number at the end of a nav item, in its tone: the decisions waiting for the visitor's answer (attention), the
 * project's active runs (running), the messages of others in the visitor's chats that they have not read (attention).
 * Hidden while it is zero or unknown.
 */
export type NavCount = "openDecisions" | "activeRuns" | "unreadChats";

/** A fold of the sidebar, its label under `nav.groups` in the message files. */
export type NavGroup = "business" | "development" | "improvement" | "knowledge" | "apps" | "settings";

/** Each fold's icon, before its label; the fold's items keep their own. */
export const NAV_GROUPS: Record<NavGroup, LucideIcon> = {
  business: Compass,
  development: Hammer,
  improvement: TrendingUp,
  knowledge: BookMarked,
  apps: LayoutGrid,
  settings: Settings,
};

/** The folds open until the visitor closes them: the project's two cycles, which hold the pages opened every day. */
export const NAV_GROUPS_OPEN: readonly NavGroup[] = ["business", "development"];

/**
 * The folds of a project's menu a plugin's page may name as its `area` (plugin.schema.json, $defs.page.area); a page
 * that names none, or one this web does not know, sits in apps.
 */
export const PLUGIN_AREAS: readonly NavGroup[] = ["business", "development", "improvement", "knowledge", "apps"];

/** The fold of a plugin's page whose manifest names `area`. */
export function pluginArea(area: string | null | undefined): NavGroup {
  return PLUGIN_AREAS.find((group) => group === area) ?? "apps";
}

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
  /** Shown in the first group, after HOME_NAV, rather than among the project's areas. */
  top?: boolean;
  /** The pages plugins add to the item's fold come before it rather than after the fold's last item. */
  pluginsBefore?: boolean;
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
 * The first group, without a label: where the visitor starts and what waits for them (decisions, chats).
 */
export const HOME_NAV: readonly HubNavItem[] = [
  { label: "home", icon: MessageCircle, href: "/" },
  { label: "today", icon: LayoutDashboard, href: "/today" },
  { label: "inbox", icon: Inbox, href: "/inbox", count: "openDecisions" },
  { label: "chats", icon: MessagesSquare, href: "/chats", count: "unreadChats", feature: "chats" },
];

export const PROJECT_NAV: readonly ProjectNavItem[] = [
  { label: "overview", icon: FolderKanban, segment: "" },
  { label: "systems", icon: Workflow, segment: "systems", top: true },
  { label: "kg", icon: Network, segment: "kg", feature: "kg", top: true },
  { label: "decide", icon: Signpost, segment: "decide" },
  { label: "signals", icon: RadioTower, segment: "signals", group: "business" },
  { label: "problems", icon: Bandage, segment: "problems", group: "business" },
  { label: "opportunities", icon: Telescope, segment: "opportunities", group: "business" },
  { label: "goals", icon: Goal, segment: "goals", group: "business" },
  { label: "plans", icon: ListChecks, segment: "plans", group: "development", pluginsBefore: true },
  { label: "runs", icon: Activity, segment: "runs", count: "activeRuns", group: "development" },
  { label: "loop", icon: IterationCw, segment: "loop", group: "development" },
  { label: "mergeQueue", icon: GitMerge, segment: "merge-queue", group: "development" },
  { label: "releases", icon: Tags, segment: "releases", group: "development" },
  { label: "cronJobs", icon: AlarmClock, segment: "cron-jobs", group: "development" },
  { label: "insights", icon: ChartColumn, segment: "insights", group: "improvement" },
  { label: "curator", icon: MoonStar, segment: "curator", feature: "curator", group: "improvement" },
  { label: "agents", icon: Bot, segment: "agents", group: "knowledge" },
  { label: "memories", icon: BookOpen, segment: "memories", group: "knowledge" },
  { label: "skills", icon: Sparkles, segment: "skills", group: "knowledge" },
  { label: "browser", icon: Globe, segment: "browser", group: "knowledge" },
  { label: "plugins", icon: Puzzle, segment: "plugins", group: "apps", pluginsBefore: true },
];

export const HUB_NAV: readonly HubNavItem[] = [
  { label: "workers", icon: Server, href: "/workers" },
  { label: "org", icon: Building2, href: "/org" },
  { label: "myMemories", icon: Brain, href: "/memories" },
  { label: "globalSkills", icon: Sparkles, href: "/skills" },
  { label: "market", icon: Store, href: "/market", feature: "market" },
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

const FOLDS = Object.keys(NAV_GROUPS) as NavGroup[];

/**
 * The open folds of a cookie value: those of NAV_GROUPS_OPEN, plus each it names, less each it names after a minus (a
 * fold of NAV_GROUPS_OPEN the visitor closed). Names this web does not know are left out; a value it cannot read is
 * the folds of NAV_GROUPS_OPEN.
 */
export function parseNavGroups(value: string | undefined): NavGroup[] {
  const open = new Set<string>(NAV_GROUPS_OPEN);
  let names: string[] = [];
  try {
    names = value ? decodeURIComponent(value).split(",") : [];
  } catch {
    names = [];
  }
  for (const name of names) {
    if (name.startsWith("-")) open.delete(name.slice(1));
    else open.add(name);
  }
  return FOLDS.filter((group) => open.has(group));
}

/** Keeps for a year, for every page of the hub, how the open folds differ from NAV_GROUPS_OPEN. */
export function writeNavGroups(open: Iterable<NavGroup>): void {
  const shown = new Set(open);
  const names = FOLDS.flatMap((group) => {
    const first = NAV_GROUPS_OPEN.includes(group);
    if (shown.has(group) !== first) return [first ? `-${group}` : group];
    return [];
  });
  const value = encodeURIComponent(names.sort().join(","));
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
