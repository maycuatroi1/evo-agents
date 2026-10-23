import type { AdminSection, OverviewPage } from "./admin-nav";

type Crumb = { label: string; href?: string };
type CrumbSection = Exclude<AdminSection, "overview"> | OverviewPage;

const SECTIONS: readonly string[] = [
  "members",
  "tokens",
  "audit",
  "invites",
  "plugins",
  "branding",
  "features",
  "diagnostics",
] satisfies CrumbSection[];

function isCrumbSection(value: string | undefined): value is CrumbSection {
  return SECTIONS.includes(value ?? "");
}

/**
 * The header's breadcrumbs inside /admin: the area, then the section (or a page the overview links to, such as
 * diagnostics), then a member's login or a plugin's id. `rest` is the path after /admin, split on slashes.
 */
export function adminCrumbs(rest: string[], adminLabel: string, sectionLabel: (section: CrumbSection) => string): Crumb[] {
  const [section, item] = rest;
  if (!isCrumbSection(section)) return [{ label: adminLabel }];
  const crumbs: Crumb[] = [{ label: adminLabel, href: "/admin" }, { label: sectionLabel(section) }];
  if ((section === "members" || section === "plugins") && item) {
    crumbs[1].href = `/admin/${section}`;
    crumbs.push({ label: decodeURIComponent(item) }); // the current page: no link
  }
  return crumbs;
}
