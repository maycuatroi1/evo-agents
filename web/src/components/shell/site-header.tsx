"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { Fragment } from "react";

import { adminCrumbs } from "@/components/admin/crumbs";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { Separator } from "@/components/ui/separator";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { useWorkerName } from "@/components/workers/hooks";

import { HUB_NAV, type NavLabel, PROJECT_NAV, projectHref } from "./nav";

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
    const crumbs: Crumb[] = [{ label: t("projects"), href: "/" }, { label: project }];
    if (section) {
      crumbs[1].href = projectHref(project);
      crumbs.push({ label: t(section.label satisfies NavLabel) });
      if (parts[3]) {
        // A page inside the section (a plan, a memory, a skill): the section links back to its list, the item is
        // named by its id (a number as #id), and links to itself when a page below it (a step) is shown.
        crumbs[2].href = projectHref(project, section.segment);
        crumbs.push({
          label: itemLabel(parts[3]),
          href: parts[4] ? projectHref(project, `${section.segment}/${parts[3]}`) : undefined,
        });
      }
    }
    return crumbs;
  }
  if (parts[0] === "admin") return adminCrumbs(parts.slice(1), t("admin"), tAdmin);
  const hub = HUB_NAV.find((item) => item.href !== "/" && parts[0] === item.href.slice(1));
  if (hub && parts.length > 1) {
    return [{ label: t(hub.label), href: hub.href }, { label: workerName ?? itemLabel(parts[1]) }];
  }
  if (hub) return [{ label: t(hub.label) }];
  return [{ label: t("projects") }];
}

export function SiteHeader() {
  const t = useTranslations("nav");
  const crumbs = useCrumbs();
  return (
    <header className="sticky top-0 z-10 flex h-14 shrink-0 items-center gap-2 border-b bg-background/95 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/80 md:px-6">
      <SidebarTrigger label={t("toggleSidebar")} className="-ml-1 size-9" />
      <Separator orientation="vertical" className="mr-1 data-vertical:h-5 data-vertical:self-center" />
      <Breadcrumb aria-label={t("breadcrumb")} className="min-w-0">
        <BreadcrumbList className="flex-nowrap">
          {crumbs.map((crumb, index) => (
            <Fragment key={`${index}-${crumb.label}`}>
              {index > 0 ? <BreadcrumbSeparator /> : null}
              <BreadcrumbItem className="min-w-0">
                {crumb.href ? (
                  <BreadcrumbLink asChild>
                    <Link href={crumb.href as "/"} className="truncate">
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
    </header>
  );
}
