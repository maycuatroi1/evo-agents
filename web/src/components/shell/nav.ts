import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  BookOpen,
  Bot,
  Brain,
  Building2,
  ChartColumn,
  FolderKanban,
  GitMerge,
  Goal,
  Inbox,
  IterationCw,
  LayoutDashboard,
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
  Shield,
  Signpost,
  Sparkles,
  Tags,
  UserRoundCog,
} from "lucide-react";
import type { Route } from "next";

import { browserApi } from "@/lib/api/browser";
import { type FeatureId, featureOn, featuresQuery } from "@/lib/hub-settings";

/**
 * The sidebar's links, in the kit's AppShell order: Home, Inbox and Monitor; the current project's areas, then the pages
 * its enabled plugins add (shell/plugin-nav.tsx) before its Plugins; the hub-wide areas
 * (chats, org chart, workers, secrets, accounts, memories, skills, administration). A step that adds a project area adds one entry to PROJECT_NAV,
 * a hub-wide area one entry to HUB_NAV, and one label under `nav` in messages/vi.json and messages/en.json. Icons follow
 * the Iconography table of web/DESIGN.md. An item that names a `feature` leaves the sidebar and the palette while a
 * hub admin has that feature off, for the hub or for the project shown.
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
  | "goals"
  | "loop"
  | "curator"
  | "releases"
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

export type ProjectNavItem = {
  label: NavLabel;
  icon: LucideIcon;
  /** Path below /p/{project}; "" is the project's overview. */
  segment: string;
  count?: NavCount;
  /** The feature a hub admin may turn off for the hub or the project, which hides the item (lib/hub-settings.ts). */
  feature?: FeatureId;
};

export type HubNavItem = {
  label: NavLabel;
  icon: LucideIcon;
  href: Route;
  adminOnly?: boolean;
  count?: NavCount;
  /** The feature a hub admin may turn off for the hub, which hides the item. */
  feature?: FeatureId;
};

/** The first group, without a label: where the visitor starts, what waits for them, and the runs they watch live. */
export const HOME_NAV: readonly HubNavItem[] = [
  { label: "home", icon: LayoutDashboard, href: "/" },
  { label: "inbox", icon: Inbox, href: "/inbox", count: "openDecisions" },
  { label: "monitor", icon: MonitorPlay, href: "/monitor", count: "runningRuns", feature: "monitor" },
];

export const PROJECT_NAV: readonly ProjectNavItem[] = [
  { label: "overview", icon: FolderKanban, segment: "" },
  { label: "decide", icon: Signpost, segment: "decide" },
  { label: "plans", icon: ListChecks, segment: "plans" },
  { label: "runs", icon: Activity, segment: "runs", count: "activeRuns" },
  { label: "mergeQueue", icon: GitMerge, segment: "merge-queue" },
  { label: "insights", icon: ChartColumn, segment: "insights" },
  { label: "signals", icon: RadioTower, segment: "signals" },
  { label: "goals", icon: Goal, segment: "goals" },
  { label: "loop", icon: IterationCw, segment: "loop" },
  { label: "curator", icon: MoonStar, segment: "curator", feature: "curator" },
  { label: "releases", icon: Tags, segment: "releases" },
  { label: "agents", icon: Bot, segment: "agents" },
  { label: "memories", icon: BookOpen, segment: "memories" },
  { label: "skills", icon: Sparkles, segment: "skills" },
  { label: "kg", icon: Network, segment: "kg", feature: "kg" },
  { label: "plugins", icon: Puzzle, segment: "plugins" },
];

export const HUB_NAV: readonly HubNavItem[] = [
  { label: "chats", icon: MessagesSquare, href: "/chats", count: "unreadChats", feature: "chats" },
  { label: "org", icon: Building2, href: "/org" },
  { label: "workers", icon: Server, href: "/workers" },
  { label: "secrets", icon: LockKeyhole, href: "/secrets" },
  { label: "accounts", icon: UserRoundCog, href: "/accounts" },
  { label: "myMemories", icon: Brain, href: "/memories" },
  { label: "globalSkills", icon: Sparkles, href: "/skills" },
  { label: "admin", icon: Shield, href: "/admin", adminOnly: true },
];

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
