import {
  Activity,
  BookOpen,
  Bot,
  Brain,
  ChartColumn,
  FolderKanban,
  Inbox,
  LayoutDashboard,
  ListChecks,
  LockKeyhole,
  type LucideIcon,
  MessagesSquare,
  MonitorPlay,
  MoonStar,
  Network,
  Server,
  Shield,
  Sparkles,
  UserRoundCog,
} from "lucide-react";
import type { Route } from "next";

/**
 * The sidebar's links, in the kit's AppShell order: Home, Inbox and Monitor; the current project's areas; the hub-wide areas
 * (chats, workers, secrets, accounts, memories, skills, administration). A step that adds a project area adds one entry to PROJECT_NAV,
 * a hub-wide area one entry to HUB_NAV, and one label under `nav` in messages/vi.json and messages/en.json. Icons follow
 * the Iconography table of web/DESIGN.md.
 */
export type NavLabel =
  | "home"
  | "inbox"
  | "monitor"
  | "overview"
  | "plans"
  | "runs"
  | "insights"
  | "curator"
  | "agents"
  | "chats"
  | "memories"
  | "skills"
  | "kg"
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
};

export type HubNavItem = {
  label: NavLabel;
  icon: LucideIcon;
  href: Route;
  adminOnly?: boolean;
  count?: NavCount;
};

/** The first group, without a label: where the visitor starts, what waits for them, and the runs they watch live. */
export const HOME_NAV: readonly HubNavItem[] = [
  { label: "home", icon: LayoutDashboard, href: "/" },
  { label: "inbox", icon: Inbox, href: "/inbox", count: "openDecisions" },
  { label: "monitor", icon: MonitorPlay, href: "/monitor", count: "runningRuns" },
];

export const PROJECT_NAV: readonly ProjectNavItem[] = [
  { label: "overview", icon: FolderKanban, segment: "" },
  { label: "plans", icon: ListChecks, segment: "plans" },
  { label: "runs", icon: Activity, segment: "runs", count: "activeRuns" },
  { label: "insights", icon: ChartColumn, segment: "insights" },
  { label: "curator", icon: MoonStar, segment: "curator" },
  { label: "agents", icon: Bot, segment: "agents" },
  { label: "memories", icon: BookOpen, segment: "memories" },
  { label: "skills", icon: Sparkles, segment: "skills" },
  { label: "kg", icon: Network, segment: "kg" },
];

export const HUB_NAV: readonly HubNavItem[] = [
  { label: "chats", icon: MessagesSquare, href: "/chats", count: "unreadChats" },
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

/** A count as a nav item shows it: up to 99, then 99+. */
export function countText(count: number): string {
  return count > 99 ? "99+" : String(count);
}
