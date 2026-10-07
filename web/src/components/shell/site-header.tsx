"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { Fragment } from "react";

import { adminCrumbs } from "@/components/admin/crumbs";
import { InboxBell } from "@/components/inbox/inbox-bell";
import { LiveIndicator } from "@/components/live/live-indicator";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { useWorkerName } from "@/components/workers/hooks";

import { HOME_NAV, HUB_NAV, type NavLabel, PROJECT_NAV, projectHref } from "./nav";

type Crumb = { label: string; href?: string };

/** The last part of a page's path as a crumb: a name as it is, a number as #id. */
function itemLabel(part: string): string {
  let text = part;
  try {
    text = decodeURIComponent(part);
  } catch {
    // a malformed escape: show the part as it is
  }
  return /^[0-9]+$/.test(text) ? `#${text}` : text;
}

function useCrumbs(): Crumb[] {
  const t = useTranslations("nav");
  const tAdmin = useTranslations("admin.nav");
  const pathname = usePathname();
  const parts = pathname.split("/").filter(Boolean);
  const workerName = useWorkerName(parts[0] === "workers" ? parts[1] : undefined);
  if (parts[0] === "p" && parts[1]) {
    const project = decodeURIComponent(parts[1]);
    const section = PROJECT_NAV.find((item) => item.segment !== "" && item.segment === parts[2]);
    // The kit's trail starts at the project (Home and the other projects are in the sidebar and the switcher).
    const crumbs: Crumb[] = [{ label: project }];
    if (section) {
      crumbs[0].href = projectHref(project);
      crumbs.push({ label: t(section.label satisfies NavLabel) });
      if (parts[3]) {
        // A page inside the section (a plan, a memory, a skill): the section links back to its list, the item is
        // named by its id (a number as #id), and links to itself when a page below it (a step) is shown.
        crumbs[1].href = projectHref(project, section.segment);
        crumbs.push({
          label: itemLabel(parts[3]),
          href: parts[4] ? projectHref(project, `${section.segment}/${parts[3]}`) : undefined,
        });
        // A run's diff is the one page below an item that names itself in the trail.
        if (section.segment === "runs" && parts[4] === "diff" && !parts[5]) crumbs.push({ label: t("runDiff") });
      }
    }
    return crumbs;
  }
  if (parts[0] === "admin") return adminCrumbs(parts.slice(1), t("admin"), tAdmin);
  const hub = [...HOME_NAV, ...HUB_NAV].find((item) => item.href !== "/" && parts[0] === item.href.slice(1));
  if (hub && parts.length > 1) {
    return [{ label: t(hub.label), href: hub.href }, { label: workerName ?? itemLabel(parts[1]) }];
  }
  if (hub) return [{ label: t(hub.label) }];
  return [{ label: t("home") }];
}

/**
 * The kit's top bar: 52 px on the surface, sticky, with the sidebar toggle, the breadcrumb (13 px, slashes between
 * crumbs, the current page in the text colour), then on the right whether the page is current (LiveIndicator, from what
 * the page registered) and the inbox bell. Search joins it in a later step.
 */
export function SiteHeader() {
  const t = useTranslations("nav");
  const crumbs = useCrumbs();
  return (
    <header
      data-testid="top-bar"
      className="sticky top-0 z-10 flex h-13 shrink-0 items-center gap-3 border-b bg-card px-4 md:px-6"
    >
      <SidebarTrigger label={t("toggleSidebar")} size="icon" className="-ml-1.5" />
      <Breadcrumb aria-label={t("breadcrumb")} className="min-w-0">
        <BreadcrumbList className="flex-nowrap gap-1.5 text-[13px] text-muted-foreground">
          {crumbs.map((crumb, index) => (
            <Fragment key={`${index}-${crumb.label}`}>
              {index > 0 ? <BreadcrumbSeparator className="text-fg-subtle">/</BreadcrumbSeparator> : null}
              <BreadcrumbItem className="min-w-0">
                {crumb.href ? (
                  <BreadcrumbLink asChild>
                    <Link href={crumb.href as "/"} className="truncate rounded-xs">
                      {crumb.label}
                    </Link>
                  </BreadcrumbLink>
                ) : (
                  <BreadcrumbPage className="truncate font-medium">{crumb.label}</BreadcrumbPage>
                )}
              </BreadcrumbItem>
            </Fragment>
          ))}
        </BreadcrumbList>
      </Breadcrumb>
      <div className="ml-auto flex min-w-0 shrink-0 items-center gap-1">
        <LiveIndicator />
        <InboxBell />
      </div>
    </header>
  );
}
