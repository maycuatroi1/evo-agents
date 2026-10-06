"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronsUpDown, FolderGit2, TriangleAlert } from "lucide-react";
import { useParams, useRouter } from "next/navigation";
import { useTranslations } from "next-intl";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { SidebarMenu, SidebarMenuButton, SidebarMenuItem, useSidebar } from "@/components/ui/sidebar";
import { browserApi } from "@/lib/api/browser";
import { projectsQuery } from "@/lib/queries";
import { initials } from "@/lib/utils";

import { projectHref } from "./nav";
import { roleLabelKey } from "./role-badge";

/**
 * The kit's switch, shared by the project switcher and the account menu: a bordered surface control 48 px tall, the
 * name over a subtle second line; folded to its 24 px tile in a 32 px square.
 */
export const SWITCH_CLASS =
  "gap-2 border border-border bg-card px-2 py-1.5 hover:bg-accent data-[state=open]:bg-accent data-[state=open]:text-foreground group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:border-transparent group-data-[collapsible=icon]:bg-transparent";

export function useCurrentProject(): string | null {
  const params = useParams<{ project?: string }>();
  return typeof params.project === "string" ? decodeURIComponent(params.project) : null;
}

/**
 * The project picker: exactly the projects GET /v1/projects returns for the visitor (their grants; every project
 * for a hub admin), never a list of its own. Choosing one opens its overview.
 */
export function ProjectSwitcher() {
  const t = useTranslations("projectSwitcher");
  const tRoles = useTranslations("roles");
  const router = useRouter();
  const { isMobile, setOpenMobile } = useSidebar();
  const current = useCurrentProject();
  const { data: projects, isError } = useQuery(projectsQuery(browserApi));
  const selected = projects?.find((project) => project.name === current) ?? null;

  const open = (name: string) => {
    if (isMobile) setOpenMobile(false);
    router.push(projectHref(name));
  };

  return (
    <SidebarMenu>
      <SidebarMenuItem>
        {/* Not modal: a menu leaves the page readable and reachable instead of hiding it from assistive tech. */}
        <DropdownMenu modal={false}>
          <DropdownMenuTrigger asChild>
            <SidebarMenuButton
              size="lg"
              data-testid="project-switcher"
              aria-label={selected ? t("trigger", { name: selected.name }) : t("triggerEmpty")}
              className={SWITCH_CLASS}
            >
              <span
                aria-hidden="true"
                className="flex size-6 shrink-0 items-center justify-center rounded-xs bg-foreground text-[11px] font-semibold text-background [&_svg]:size-3.5!"
              >
                {selected ? initials(selected.name) : <FolderGit2 />}
              </span>
              <span className="grid min-w-0 flex-1 text-left group-data-[collapsible=icon]:hidden">
                <span className="truncate text-[13px] leading-[18px] font-semibold text-foreground">
                  {selected ? selected.name : t("placeholder")}
                </span>
                <span className="truncate text-xs leading-4 font-normal text-fg-subtle">
                  {selected ? tRoles(roleLabelKey(selected.role)) : t("hint")}
                </span>
              </span>
              <ChevronsUpDown className="ml-auto text-fg-subtle group-data-[collapsible=icon]:hidden" aria-hidden="true" />
            </SidebarMenuButton>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            className="w-(--radix-dropdown-menu-trigger-width) min-w-64 rounded-md"
            align="start"
            side={isMobile ? "bottom" : "right"}
            sideOffset={4}
            data-testid="project-switcher-menu"
          >
            <DropdownMenuLabel className="text-xs text-muted-foreground">{t("label")}</DropdownMenuLabel>
            {isError ? (
              <DropdownMenuItem disabled>
                <TriangleAlert aria-hidden="true" />
                {t("loadError")}
              </DropdownMenuItem>
            ) : projects && projects.length > 0 ? (
              <DropdownMenuRadioGroup value={selected?.name ?? ""} onValueChange={open}>
                {projects.map((project) => (
                  <DropdownMenuRadioItem
                    key={project.name}
                    value={project.name}
                    data-project={project.name}
                    className="gap-2 py-2"
                  >
                    <span className="truncate font-medium">{project.name}</span>
                    <span className="ml-auto pl-2 text-xs text-muted-foreground">
                      {tRoles(roleLabelKey(project.role))}
                    </span>
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            ) : (
              <DropdownMenuItem disabled>{t("none")}</DropdownMenuItem>
            )}
            <DropdownMenuSeparator />
            <DropdownMenuItem
              onSelect={() => {
                if (isMobile) setOpenMobile(false);
                router.push("/");
              }}
            >
              <FolderGit2 aria-hidden="true" />
              {t("allProjects")}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarMenuItem>
    </SidebarMenu>
  );
}
