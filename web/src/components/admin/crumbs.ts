import type { AdminSection } from "./admin-nav";

type Crumb = { label: string; href?: string };

/**
 * The header's breadcrumbs inside /admin: the area, then the section, then a member's login. `rest` is the path after
 * /admin, split on slashes.
 */
export function adminCrumbs(
  rest: string[],
  adminLabel: string,
  sectionLabel: (section: Exclude<AdminSection, "overview">) => string,
): Crumb[] {
  const [section, item] = rest;
  if (section !== "members" && section !== "tokens" && section !== "audit") return [{ label: adminLabel }];
  const crumbs: Crumb[] = [{ label: adminLabel, href: "/admin" }, { label: sectionLabel(section) }];
  if (section === "members" && item) {
    crumbs[1].href = "/admin/members";
    crumbs.push({ label: decodeURIComponent(item) }); // the current page: no link
  }
  return crumbs;
}
