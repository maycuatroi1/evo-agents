"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ReactNode, useId, useState } from "react";

import { HubMark } from "@/components/brand";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
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
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { browserApi } from "@/lib/api/browser";
import { brandingQuery } from "@/lib/hub-settings";
import { whoamiQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { FleetLine } from "./fleet-line";
import {
  countText,
  HOME_NAV,
  HUB_NAV,
  isActive,
  NAV_GROUPS,
  type NavCount,
  type NavGroup,
  navRows,
  PROJECT_NAV,
  projectHref,
  useNavShown,
  writeNavGroups,
} from "./nav";
import { useNavCounts } from "./nav-counts";
import { usePluginNavLinks } from "./plugin-nav";
import { ProjectSwitcher, useSelectedProject } from "./project-switcher";
import { NAV_SHORTCUTS, ShortcutKeys, type ShortcutId } from "./shortcuts";
import { UserMenu } from "./user-menu";

/** The tone of each count: a person is needed (attention), an agent is working (running). */
const COUNT_TONE: Record<NavCount, string> = {
  openDecisions: "bg-attention-soft text-attention",
  activeRuns: "bg-running-soft text-running",
  runningRuns: "bg-running-soft text-running",
  unreadChats: "bg-attention-soft text-attention",
};
/** The same tone as a dot on the icon when the sidebar is folded and the number has no room. */
const COUNT_DOT: Record<NavCount, string> = {
  openDecisions: "bg-attention-solid",
  activeRuns: "bg-running",
  runningRuns: "bg-running",
  unreadChats: "bg-attention-solid",
};

/** A link of the sidebar as it draws it: an area of nav.ts, or a page a plugin adds. */
type NavLink = {
  key: string;
  testId: string;
  name: string;
  icon: ReactNode;
  href: Route;
  active: boolean;
  shortcut?: ShortcutId;
  count?: NavCount;
  value: number | null;
  group?: NavGroup;
};

/**
 * One nav item of the kit: icon and label, surface-selected with a brand icon when it is the page shown, and its count
 * at the end in its tone. The number is drawn for the eye; screen readers hear it in words after the label, through
 * the link's name (browsers put a space between flex items, so the words are not left to the text content). An item
 * that G and a letter open shows those keys while the pointer is over it or it has keyboard focus (shell/shortcuts.tsx).
 */
function NavItem({ link, onNavigate }: { link: NavLink; onNavigate: () => void }) {
  const t = useTranslations("nav");
  const { count, value } = link;
  const shown = count !== undefined && value !== null && value > 0 ? value : null;
  const name = count !== undefined && shown !== null ? `${link.name}, ${t(`counts.${count}`, { count: shown })}` : undefined;
  return (
    <SidebarMenuItem>
      <SidebarMenuButton asChild isActive={link.active} tooltip={link.name}>
        <Link
          href={link.href}
          aria-current={link.active ? "page" : undefined}
          aria-label={name}
          onClick={onNavigate}
          data-testid={link.testId}
          className="relative"
        >
          {link.icon}
          <span className="min-w-0 flex-1 truncate">{link.name}</span>
          {link.shortcut ? (
            <ShortcutKeys
              id={link.shortcut}
              className="hidden group-data-[collapsible=icon]:hidden! md:group-hover/menu-item:inline-flex md:group-has-[:focus-visible]/menu-item:inline-flex"
            />
          ) : null}
          {count !== undefined && shown !== null ? (
            <>
              <span
                aria-hidden="true"
                data-testid={`${link.testId}-count`}
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

type FoldProps = {
  group: NavGroup;
  links: NavLink[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onNavigate: () => void;
};

/**
 * A fold of the sidebar (nav.ts, NAV_GROUPS): a row with the fold's icon, label and a chevron that opens its items
 * below, indented on a guide line. Closed while it holds the page shown, the row is marked as that page's. On the folded
 * desktop sidebar the row is an icon that opens the items in a menu beside it, the page shown marked there. The items
 * stay in the page while the fold is closed, hidden, so a page or a test can find each one.
 */
function NavFold({ group, links, open, onOpenChange, onNavigate }: FoldProps) {
  const t = useTranslations("nav");
  const { state, isMobile } = useSidebar();
  const Icon = NAV_GROUPS[group];
  const label = t(`groups.${group}`);
  const holdsPage = links.some((link) => link.active);
  const itemsId = useId();

  if (state === "collapsed" && !isMobile) {
    return (
      <SidebarMenuItem data-nav-group={group}>
        <DropdownMenu modal={false}>
          <Tooltip>
            <TooltipTrigger asChild>
              <DropdownMenuTrigger asChild>
                <SidebarMenuButton isActive={holdsPage} data-testid={`nav-group-${group}`}>
                  <Icon aria-hidden="true" />
                  <span>{label}</span>
                </SidebarMenuButton>
              </DropdownMenuTrigger>
            </TooltipTrigger>
            <TooltipContent side="right" align="center">
              {label}
            </TooltipContent>
          </Tooltip>
          <DropdownMenuContent side="right" align="start" sideOffset={8} className="w-56">
            <DropdownMenuLabel className="text-[11px] leading-4 font-semibold tracking-[0.06em] text-fg-subtle uppercase">
              {label}
            </DropdownMenuLabel>
            {links.map((link) => (
              <DropdownMenuItem key={link.key} asChild>
                <Link
                  href={link.href}
                  aria-current={link.active ? "page" : undefined}
                  onClick={onNavigate}
                  data-testid={link.testId}
                  className="cursor-pointer aria-[current=page]:bg-sidebar-accent aria-[current=page]:[&>svg:first-child]:text-brand"
                >
                  {link.icon}
                  <span className="min-w-0 flex-1 truncate">{link.name}</span>
                </Link>
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarMenuItem>
    );
  }

  return (
    <SidebarMenuItem data-nav-group={group}>
      <SidebarMenuButton
        type="button"
        isActive={!open && holdsPage}
        aria-expanded={open}
        aria-controls={itemsId}
        data-state={open ? "open" : "closed"}
        onClick={() => onOpenChange(!open)}
        data-testid={`nav-group-${group}`}
      >
        <Icon aria-hidden="true" />
        <span className="min-w-0 flex-1 truncate">{label}</span>
        <ChevronRight
          aria-hidden="true"
          className="text-fg-subtle transition-transform duration-fast ease-standard group-data-[state=open]/menu-button:rotate-90 motion-reduce:transition-none"
        />
      </SidebarMenuButton>
      {/* In the page while closed, hidden: the closed fold's links stay findable. */}
      <div id={itemsId} hidden={!open}>
        <SidebarMenu className="mt-px ml-4 w-auto gap-px border-l border-sidebar-border pl-2">
          {links.map((link) => (
            <NavItem key={link.key} link={link} onNavigate={onNavigate} />
          ))}
        </SidebarMenu>
      </div>
    </SidebarMenuItem>
  );
}

/**
 * The sidebar of the kit's AppShell: the mark and name (the hub's branding's when it sets them), the project switcher,
 * Home, Inbox, Monitor, Chats and the selected project's knowledge graph, the project's other areas and the pages its
 * plugins add, the hub's areas, then the
 * fleet line and the account at the foot. An area whose feature a hub admin turned off, for the hub or the project
 * shown, is left out. 240 px wide, folding to 56 px of icons on desktop (the header button, Ctrl or Cmd + B) and a
 * sheet under 768 px.
 *
 * The areas a visitor opens less often sit in folds (nav.ts, NAV_GROUPS). A fold is open while the visitor opened it
 * (`openGroups`, from the cookie, kept as they change it) or while it holds the page shown, unless they closed it on
 * that page; leaving the page closes a fold they did not open themselves, so the sidebar stays short.
 */
export function AppSidebar({ openGroups = [] }: { openGroups?: readonly NavGroup[] }) {
  const t = useTranslations("nav");
  const tApp = useTranslations("app");
  const pathname = usePathname();
  const project = useSelectedProject();
  const counts = useNavCounts(project);
  const { isMobile, setOpenMobile } = useSidebar();
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const { data: branding } = useQuery(brandingQuery(browserApi));
  const shown = useNavShown();
  const pluginLinks = usePluginNavLinks(project, pathname);
  const [kept, setKept] = useState<ReadonlySet<NavGroup>>(() => new Set(openGroups));
  const [shut, setShut] = useState<{ group: NavGroup; path: string } | null>(null);
  const closeOnMobile = () => {
    if (isMobile) setOpenMobile(false);
  };

  const hubLink = (item: (typeof HUB_NAV)[number]): NavLink => ({
    key: item.label,
    testId: `nav-${item.label}`,
    name: t(item.label),
    icon: <item.icon aria-hidden="true" />,
    href: item.href,
    active: isActive(pathname, item.href, false),
    shortcut: NAV_SHORTCUTS[item.label],
    count: item.count,
    value: item.count ? counts[item.count] : null,
    group: item.group,
  });
  const projectLink = (item: (typeof PROJECT_NAV)[number], at: string): NavLink => {
    const href = projectHref(at, item.segment);
    return {
      key: item.label,
      testId: `nav-${item.label}`,
      name: t(item.label),
      icon: <item.icon aria-hidden="true" />,
      href,
      active: isActive(pathname, href, item.segment === ""),
      shortcut: NAV_SHORTCUTS[item.label],
      count: item.count,
      value: item.count ? counts[item.count] : null,
      group: item.group,
    };
  };
  // The project's areas marked `top` (the knowledge graph) follow Home, Inbox, Monitor and Chats.
  const homeLinks = [
    ...HOME_NAV.filter((item) => shown(item)).map(hubLink),
    ...(project ? PROJECT_NAV.filter((item) => item.top && shown(item, project)).map((item) => projectLink(item, project)) : []),
  ];
  const projectLinks: NavLink[] = project
    ? PROJECT_NAV.filter((item) => !item.top && shown(item, project)).flatMap((item) => {
        const link = projectLink(item, project);
        // the pages the project's plugins add come after its own areas, before its Plugins, in its fold
        if (item.label !== "plugins") return [link];
        return [...pluginLinks.map((page) => ({ ...page, value: null, group: item.group })), link];
      })
    : [];
  const hubLinks = HUB_NAV.filter((item) => (!item.adminOnly || me?.admin) && shown(item)).map(hubLink);

  // The fold of the page shown; a plugin's page is known by its path before the plugins are read, so it renders open.
  const pluginFold = PROJECT_NAV.find((item) => item.label === "plugins")?.group;
  const pageFold =
    [...projectLinks, ...hubLinks].find((link) => link.active && link.group)?.group ??
    (project && pathname.startsWith(`${projectHref(project)}/x/`) ? pluginFold : undefined);
  const isOpen = (group: NavGroup) =>
    kept.has(group) || (group === pageFold && !(shut?.group === group && shut.path === pathname));
  const setFold = (group: NavGroup, open: boolean) => {
    const next = new Set(kept);
    if (open) next.add(group);
    else next.delete(group);
    setKept(next);
    writeNavGroups(next);
    setShut(!open && group === pageFold ? { group, path: pathname } : null);
  };

  const rows = (links: NavLink[]) =>
    navRows(links).map((row) =>
      row.kind === "item" ? (
        <NavItem key={row.item.key} link={row.item} onNavigate={closeOnMobile} />
      ) : (
        <NavFold
          key={`group-${row.group}`}
          group={row.group}
          links={row.items}
          open={isOpen(row.group)}
          onOpenChange={(open) => setFold(row.group, open)}
          onNavigate={closeOnMobile}
        />
      ),
    );

  return (
    <Sidebar collapsible="icon" mobileTitle={t("label")} mobileDescription={tApp("description")}>
      <SidebarHeader className="gap-3 p-3 pb-2">
        <Link
          href="/"
          onClick={closeOnMobile}
          data-testid="brand"
          className="flex h-8 items-center gap-2 rounded-sm px-1 text-sm font-semibold tracking-[-0.01em] text-foreground group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0"
        >
          <HubMark logoUrl={branding?.logo_url} className="size-6" />
          <span className="truncate group-data-[collapsible=icon]:sr-only">{branding?.name ?? tApp("name")}</span>
        </Link>
        <ProjectSwitcher />
      </SidebarHeader>
      <SidebarContent className="px-3 py-1">
        <nav aria-label={t("label")} className="flex flex-col gap-3">
          <SidebarMenu className="gap-px">{rows(homeLinks)}</SidebarMenu>
          {project ? (
            <SidebarGroup className="p-0">
              <SidebarGroupLabel>{t("project")}</SidebarGroupLabel>
              <SidebarMenu className="gap-px">{rows(projectLinks)}</SidebarMenu>
            </SidebarGroup>
          ) : null}
          <SidebarGroup className="p-0">
            <SidebarGroupLabel>{t("hub")}</SidebarGroupLabel>
            <SidebarMenu className="gap-px">{rows(hubLinks)}</SidebarMenu>
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
