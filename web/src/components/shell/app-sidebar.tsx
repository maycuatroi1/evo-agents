"use client";

import { useQuery } from "@tanstack/react-query";
import type { LucideIcon } from "lucide-react";
import type { Route } from "next";
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
import { cn } from "@/lib/utils";

import { FleetLine } from "./fleet-line";
import { countText, HOME_NAV, HUB_NAV, isActive, type NavCount, type NavLabel, PROJECT_NAV, projectHref } from "./nav";
import { useNavCounts } from "./nav-counts";
import { ProjectSwitcher, useCurrentProject } from "./project-switcher";
import { NAV_SHORTCUTS, ShortcutKeys } from "./shortcuts";
import { UserMenu } from "./user-menu";

/** The tone of each count: a person is needed (attention), an agent is working (running). */
const COUNT_TONE: Record<NavCount, string> = {
  openDecisions: "bg-attention-soft text-attention",
  activeRuns: "bg-running-soft text-running",
  runningRuns: "bg-running-soft text-running",
};
/** The same tone as a dot on the icon when the sidebar is folded and the number has no room. */
const COUNT_DOT: Record<NavCount, string> = {
  openDecisions: "bg-attention-solid",
  activeRuns: "bg-running",
  runningRuns: "bg-running",
};

type ItemProps = {
  label: NavLabel;
  icon: LucideIcon;
  href: Route;
  active: boolean;
  count?: NavCount;
  value: number | null;
  onNavigate: () => void;
};

/**
 * One nav item of the kit: icon and label, surface-selected with a brand icon when it is the page shown, and its count
 * at the end in its tone. The number is drawn for the eye; screen readers hear it in words after the label, through
 * the link's name (browsers put a space between flex items, so the words are not left to the text content). An item
 * that G and a letter open shows those keys while the pointer is over it or it has keyboard focus (shell/shortcuts.tsx).
 */
function NavItem({ label, icon: Icon, href, active, count, value, onNavigate }: ItemProps) {
  const t = useTranslations("nav");
  const shortcut = NAV_SHORTCUTS[label];
  const shown = count !== undefined && value !== null && value > 0 ? value : null;
  const name = count !== undefined && shown !== null ? `${t(label)}, ${t(`counts.${count}`, { count: shown })}` : undefined;
  return (
    <SidebarMenuItem>
      <SidebarMenuButton asChild isActive={active} tooltip={t(label)}>
        <Link
          href={href}
          aria-current={active ? "page" : undefined}
          aria-label={name}
          onClick={onNavigate}
          data-testid={`nav-${label}`}
          className="relative"
        >
          <Icon aria-hidden="true" />
          <span className="min-w-0 flex-1 truncate">{t(label)}</span>
          {shortcut ? (
            <ShortcutKeys
              id={shortcut}
              className="hidden group-data-[collapsible=icon]:hidden! md:group-hover/menu-item:inline-flex md:group-has-[:focus-visible]/menu-item:inline-flex"
            />
          ) : null}
          {count !== undefined && shown !== null ? (
            <>
              <span
                aria-hidden="true"
                data-testid={`nav-${label}-count`}
                className={cn(
                  "inline-flex h-4.5 min-w-4.5 shrink-0 items-center justify-center rounded-full px-1.25 text-[11px] leading-none font-medium tabular-nums group-data-[collapsible=icon]:hidden",
                  COUNT_TONE[count],
                )}
              >
                {countText(shown)}
              </span>
              <span
                aria-hidden="true"
                className={cn(
                  "absolute top-1 right-1 hidden size-1.5 rounded-full group-data-[collapsible=icon]:block",
                  COUNT_DOT[count],
                )}
              />
            </>
          ) : null}
        </Link>
      </SidebarMenuButton>
    </SidebarMenuItem>
  );
}

/**
 * The sidebar of the kit's AppShell: the mark and name, the project switcher, Home and Inbox, the current project's
 * areas, the hub's areas, then the fleet line and the account at the foot. 240 px wide, folding to 56 px of icons on
 * desktop (the header button, Ctrl or Cmd + B) and a sheet under 768 px.
 */
export function AppSidebar() {
  const t = useTranslations("nav");
  const tApp = useTranslations("app");
  const pathname = usePathname();
  const project = useCurrentProject();
  const counts = useNavCounts(project);
  const { isMobile, setOpenMobile } = useSidebar();
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const closeOnMobile = () => {
    if (isMobile) setOpenMobile(false);
  };

  return (
    <Sidebar collapsible="icon" mobileTitle={t("label")} mobileDescription={tApp("description")}>
      <SidebarHeader className="gap-3 p-3 pb-2">
        <Link
          href="/"
          onClick={closeOnMobile}
          data-testid="brand"
          className="flex h-8 items-center gap-2 rounded-sm px-1 text-sm font-semibold tracking-[-0.01em] text-foreground group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0"
        >
          <BrandMark className="size-6" />
          <span className="truncate group-data-[collapsible=icon]:sr-only">{tApp("name")}</span>
        </Link>
        <ProjectSwitcher />
      </SidebarHeader>
      <SidebarContent className="px-3 py-1">
        <nav aria-label={t("label")} className="flex flex-col gap-3">
          <SidebarMenu className="gap-px">
            {HOME_NAV.map((item) => (
              <NavItem
                key={item.label}
                label={item.label}
                icon={item.icon}
                href={item.href}
                active={isActive(pathname, item.href, false)}
                count={item.count}
                value={item.count ? counts[item.count] : null}
                onNavigate={closeOnMobile}
              />
            ))}
          </SidebarMenu>
          {project ? (
            <SidebarGroup className="p-0">
              <SidebarGroupLabel>{t("project")}</SidebarGroupLabel>
              <SidebarMenu className="gap-px">
                {PROJECT_NAV.map((item) => {
                  const href = projectHref(project, item.segment);
                  return (
                    <NavItem
                      key={item.label}
                      label={item.label}
                      icon={item.icon}
                      href={href}
                      active={isActive(pathname, href, item.segment === "")}
                      count={item.count}
                      value={item.count ? counts[item.count] : null}
                      onNavigate={closeOnMobile}
                    />
                  );
                })}
              </SidebarMenu>
            </SidebarGroup>
          ) : null}
          <SidebarGroup className="p-0">
            <SidebarGroupLabel>{t("hub")}</SidebarGroupLabel>
            <SidebarMenu className="gap-px">
              {HUB_NAV.filter((item) => !item.adminOnly || me?.admin).map((item) => (
                <NavItem
                  key={item.label}
                  label={item.label}
                  icon={item.icon}
                  href={item.href}
                  active={isActive(pathname, item.href, false)}
                  count={item.count}
                  value={item.count ? counts[item.count] : null}
                  onNavigate={closeOnMobile}
                />
              ))}
            </SidebarMenu>
          </SidebarGroup>
        </nav>
      </SidebarContent>
      <SidebarFooter className="gap-2 p-3">
        <FleetLine onNavigate={closeOnMobile} />
        <UserMenu />
      </SidebarFooter>
      <SidebarRail aria-label={t("toggleSidebar")} title={t("toggleSidebar")} />
    </Sidebar>
  );
}
