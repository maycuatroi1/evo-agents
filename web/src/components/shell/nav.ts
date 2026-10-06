import {
  Blocks,
  Brain,
  FolderGit2,
  Inbox,
  LayoutDashboard,
  ListChecks,
  LockKeyhole,
  type LucideIcon,
  Network,
  NotebookPen,
  Play,
  Puzzle,
  Server,
  ShieldCheck,
} from "lucide-react";
import type { Route } from "next";

/**
 * The sidebar's links. Steps that add a project area (plans, runs, memories, skills, knowledge graph) add one entry to
 * PROJECT_NAV, a hub-wide area (the inbox, workers, secrets) one entry to HUB_NAV, and one label under `nav` in messages/vi.json
 * and messages/en.json.
 */
export type NavLabel =
  | "overview"
  | "projects"
  | "admin"
  | "plans"
  | "runs"
  | "memories"
  | "skills"
  | "kg"
  | "myMemories"
  | "globalSkills"
  | "workers"
  | "secrets"
  | "inbox";

export type ProjectNavItem = {
  label: NavLabel;
  icon: LucideIcon;
  /** Path below /p/{project}; "" is the project's overview. */
  segment: string;
};

export type HubNavItem = {
  label: NavLabel;
  icon: LucideIcon;
  href: Route;
  adminOnly?: boolean;
};

export const PROJECT_NAV: readonly ProjectNavItem[] = [
  { label: "overview", icon: LayoutDashboard, segment: "" },
  { label: "plans", icon: ListChecks, segment: "plans" },
  { label: "runs", icon: Play, segment: "runs" },
  { label: "memories", icon: Brain, segment: "memories" },
  { label: "skills", icon: Puzzle, segment: "skills" },
  { label: "kg", icon: Network, segment: "kg" },
];

export const HUB_NAV: readonly HubNavItem[] = [
  { label: "projects", icon: FolderGit2, href: "/" },
  { label: "inbox", icon: Inbox, href: "/inbox" },
  { label: "workers", icon: Server, href: "/workers" },
  { label: "secrets", icon: LockKeyhole, href: "/secrets" },
  { label: "myMemories", icon: NotebookPen, href: "/memories" },
  { label: "globalSkills", icon: Blocks, href: "/skills" },
  { label: "admin", icon: ShieldCheck, href: "/admin", adminOnly: true },
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
