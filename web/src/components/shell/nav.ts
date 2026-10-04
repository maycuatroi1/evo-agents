import { FolderGit2, LayoutDashboard, ListChecks, type LucideIcon, ShieldCheck } from "lucide-react";
import type { Route } from "next";

/**
 * The sidebar's links. Steps that add a project area (plans, memories, skills, knowledge graph) add one entry to
 * PROJECT_NAV and one label under `nav` in messages/vi.json and messages/en.json.
 */
export type NavLabel = "overview" | "projects" | "admin" | "plans";

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
];

export const HUB_NAV: readonly HubNavItem[] = [
  { label: "projects", icon: FolderGit2, href: "/" },
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
