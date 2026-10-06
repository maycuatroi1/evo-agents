"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";

import { BrandMark } from "@/components/brand";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
  useSidebar,
} from "@/components/ui/sidebar";
import { browserApi } from "@/lib/api/browser";
import { whoamiQuery } from "@/lib/queries";

import { HUB_NAV, isActive, PROJECT_NAV, projectHref } from "./nav";
import { ProjectSwitcher, useCurrentProject } from "./project-switcher";
import { UserMenu } from "./user-menu";

export function AppSidebar() {
  const t = useTranslations("nav");
  const tApp = useTranslations("app");
  const pathname = usePathname();
  const project = useCurrentProject();
  const { isMobile, setOpenMobile } = useSidebar();
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const closeOnMobile = () => {
    if (isMobile) setOpenMobile(false);
  };

  return (
    <Sidebar collapsible="icon" mobileTitle={t("label")} mobileDescription={tApp("description")}>
      <SidebarHeader className="gap-3">
        <Link
          href="/"
          onClick={closeOnMobile}
          className="flex items-center gap-2.5 rounded-sm px-1.5 py-1 group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0"
        >
          <BrandMark className="size-7" />
          <span className="truncate text-sm font-semibold tracking-tight group-data-[collapsible=icon]:hidden">
            {tApp("name")}
          </span>
        </Link>
        <ProjectSwitcher />
      </SidebarHeader>
      <SidebarContent>
        <nav aria-label={t("label")} className="flex flex-col">
          {project ? (
            <SidebarGroup>
              <SidebarGroupLabel>{t("project")}</SidebarGroupLabel>
              <SidebarMenu>
                {PROJECT_NAV.map((item) => {
                  const href = projectHref(project, item.segment);
                  const active = isActive(pathname, href, item.segment === "");
                  return (
                    <SidebarMenuItem key={item.label}>
                      <SidebarMenuButton asChild isActive={active} tooltip={t(item.label)}>
                        <Link
                          href={href}
                          aria-current={active ? "page" : undefined}
                          onClick={closeOnMobile}
                          data-testid={`nav-${item.label}`}
                        >
                          <item.icon aria-hidden="true" />
                          <span>{t(item.label)}</span>
                        </Link>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  );
                })}
              </SidebarMenu>
            </SidebarGroup>
          ) : null}
          <SidebarGroup>
            <SidebarGroupLabel>{t("hub")}</SidebarGroupLabel>
            <SidebarMenu>
              {HUB_NAV.filter((item) => !item.adminOnly || me?.admin).map((item) => {
                const active = isActive(pathname, item.href, false);
                return (
                  <SidebarMenuItem key={item.label}>
                    <SidebarMenuButton asChild isActive={active} tooltip={t(item.label)}>
                      <Link
                        href={item.href}
                        aria-current={active ? "page" : undefined}
                        onClick={closeOnMobile}
                        data-testid={`nav-${item.label}`}
                      >
                        <item.icon aria-hidden="true" />
                        <span>{t(item.label)}</span>
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>
          </SidebarGroup>
        </nav>
      </SidebarContent>
      <SidebarFooter>
        <UserMenu />
      </SidebarFooter>
      <SidebarRail aria-label={t("toggleSidebar")} title={t("toggleSidebar")} />
    </Sidebar>
  );
}
