"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronsUpDown, FolderGit2, LayoutDashboard, Search, TriangleAlert, X } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { type KeyboardEvent, useId, useMemo, useRef, useState } from "react";

import { useVisibilityName } from "@/components/data/visibility";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { SidebarMenu, SidebarMenuButton, SidebarMenuItem, useSidebar } from "@/components/ui/sidebar";
import { browserApi } from "@/lib/api/browser";
import type { Project } from "@/lib/api/client";
import { overviewQuery, projectsQuery } from "@/lib/queries";
import { cn, initials } from "@/lib/utils";

import { countText, projectHref } from "./nav";
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

/** The project's tile: its initials in the text colour's ink, square-ish since it names something. */
function ProjectTile({ name }: { name: string | null }) {
  return (
    <span
      aria-hidden="true"
      className="flex size-6 shrink-0 items-center justify-center rounded-xs bg-foreground text-[11px] font-semibold text-background [&_svg]:size-3.5!"
    >
      {name ? initials(name) : <FolderGit2 />}
    </span>
  );
}

/** One project as the switcher lists it: what the visitor may do there and what waits in it. */
export type SwitcherProject = {
  name: string;
  role: string | null;
  maxLevel: string | null;
  repos: number;
  /** From GET /v1/me/overview, null until it is read or for a project the visitor holds no grant on. */
  activePlans: number | null;
  openDecisions: number | null;
};

/**
 * The projects the switcher lists: exactly those GET /v1/projects returns for the visitor (their grants; every project
 * for a hub admin), with the counts of the overview where it has them (the projects of the visitor's grants).
 */
export function switcherProjects(
  projects: readonly Pick<Project, "name" | "role" | "max_level" | "repos">[],
  counts: readonly { name: string; active_plans: number; open_decisions: number; repos: number }[] | undefined,
): SwitcherProject[] {
  return projects.map((project) => {
    const known = counts?.find((item) => item.name === project.name);
    return {
      name: project.name,
      role: project.role,
      maxLevel: project.max_level,
      repos: known?.repos ?? project.repos.length,
      activePlans: known?.active_plans ?? null,
      openDecisions: known?.open_decisions ?? null,
    };
  });
}

/** The projects whose name holds every word of `query`, in the order given. */
export function matchProjects<T extends { name: string }>(projects: readonly T[], query: string): T[] {
  const words = query.toLocaleLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return [...projects];
  return projects.filter((project) => words.every((word) => project.name.toLocaleLowerCase().includes(word)));
}

function ProjectOption({ project, current, onPick }: { project: SwitcherProject; current: boolean; onPick: () => void }) {
  const t = useTranslations("projectSwitcher");
  const tRoles = useTranslations("roles");
  const visibility = useVisibilityName();
  const facts = [
    tRoles(roleLabelKey(project.role)),
    project.maxLevel ? t("visibility", { level: visibility(project.maxLevel) }) : null,
    t("repos", { count: project.repos }),
    project.activePlans !== null ? t("plans", { count: project.activePlans }) : null,
  ].filter((fact): fact is string => fact !== null);
  const decisions = project.openDecisions ?? 0;
  return (
    <li>
      <Link
        href={projectHref(project.name)}
        onClick={onPick}
        aria-current={current ? "true" : undefined}
        data-project={project.name}
        data-switcher-option=""
        className={cn(
          "flex min-h-11 items-start gap-2.5 rounded-sm px-2 py-2 text-left outline-none transition-colors hover:bg-accent focus-visible:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
          current && "bg-surface-selected hover:bg-surface-selected",
        )}
      >
        <ProjectTile name={project.name} />
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="truncate text-[13px] leading-[18px] font-semibold text-foreground" data-testid="switcher-project-name">
            {project.name}
          </span>
          <span className="text-xs leading-4 text-fg-subtle [overflow-wrap:anywhere]" data-testid="switcher-project-facts">
            {facts.join(", ")}
          </span>
        </span>
        {decisions > 0 ? (
          <span className="mt-0.5 shrink-0" data-testid="switcher-project-decisions">
            <span
              aria-hidden="true"
              className="inline-flex h-4.5 min-w-4.5 items-center justify-center rounded-full bg-attention-soft px-1.25 text-[11px] leading-none font-medium text-attention tabular-nums"
            >
              {countText(decisions)}
            </span>
            <span className="sr-only">{t("decisions", { count: decisions })}</span>
          </span>
        ) : null}
      </Link>
    </li>
  );
}

/** Moves focus among the list's links with the arrow keys, Home and End; up from the first goes back to the field. */
function moveFocus(event: KeyboardEvent<HTMLElement>, list: HTMLElement | null, field: HTMLInputElement | null) {
  if (!list) return;
  const options = [...list.querySelectorAll<HTMLAnchorElement>("[data-switcher-option]")];
  if (options.length === 0) return;
  const index = options.indexOf(document.activeElement as HTMLAnchorElement);
  let next: HTMLElement | null = null;
  if (event.key === "ArrowDown") next = options[index < 0 ? 0 : Math.min(index + 1, options.length - 1)];
  else if (event.key === "ArrowUp") next = index <= 0 ? field : options[index - 1];
  else if (event.key === "Home" && index >= 0) next = options[0];
  else if (event.key === "End" && index >= 0) next = options[options.length - 1];
  if (!next) return;
  event.preventDefault();
  next.focus();
}

/**
 * The project switcher: every project the visitor can open, found by name in the field at its top, each with the
 * visitor's role, the Visibility of their grant, its repos, its active plans and its open decisions (an amber count).
 * The projects are GET /v1/projects (never a list of its own); the counts come from GET /v1/me/overview, read when the
 * switcher opens, so a page that never opens it does not ask. Choosing a project opens its overview; Home sits at the
 * foot. The list is a navigation of links: the arrow keys move through it from the field.
 */
export function ProjectSwitcher() {
  const t = useTranslations("projectSwitcher");
  const tRoles = useTranslations("roles");
  const { isMobile, setOpenMobile } = useSidebar();
  const current = useCurrentProject();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const field = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const fieldId = useId();
  const listId = useId();
  const { data: projects, isError } = useQuery(projectsQuery(browserApi));
  const { data: overview } = useQuery({ ...overviewQuery(browserApi), enabled: open });
  const selected = projects?.find((project) => project.name === current) ?? null;
  const all = useMemo(() => (open ? switcherProjects(projects ?? [], overview?.projects) : []), [open, projects, overview]);
  const shown = matchProjects(all, query);

  const close = () => {
    setOpen(false);
    if (isMobile) setOpenMobile(false);
  };

  return (
    <SidebarMenu>
      <SidebarMenuItem>
        <Popover
          open={open}
          onOpenChange={(next) => {
            setOpen(next);
            if (!next) setQuery("");
          }}
        >
          <PopoverTrigger asChild>
            <SidebarMenuButton
              size="lg"
              data-testid="project-switcher"
              aria-label={selected ? t("trigger", { name: selected.name }) : t("triggerEmpty")}
              className={SWITCH_CLASS}
            >
              <ProjectTile name={selected?.name ?? null} />
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
          </PopoverTrigger>
          <PopoverContent
            align="start"
            side={isMobile ? "bottom" : "right"}
            sideOffset={4}
            aria-label={t("dialog")}
            className="max-h-[min(30rem,var(--radix-popover-content-available-height))] w-[min(22rem,calc(100vw-2rem))] gap-1 overflow-hidden p-1.5"
            data-testid="project-switcher-menu"
            onOpenAutoFocus={(event) => {
              event.preventDefault();
              field.current?.focus();
            }}
            onEscapeKeyDown={(event) => {
              // The first Escape in a field that holds text empties it; the next one closes the switcher.
              if (query && document.activeElement === field.current) {
                event.preventDefault();
                setQuery("");
              }
            }}
          >
            <div className="flex h-8 shrink-0 items-center gap-2 rounded-sm border border-input bg-card pr-1 pl-2.5 text-fg-subtle focus-within:border-ring focus-within:ring-1 focus-within:ring-ring max-md:h-11">
              <label htmlFor={fieldId} className="sr-only">
                {t("searchLabel")}
              </label>
              <Search className="size-4 shrink-0" aria-hidden="true" />
              <input
                ref={field}
                id={fieldId}
                type="search"
                value={query}
                maxLength={100}
                placeholder={t("search")}
                autoComplete="off"
                spellCheck={false}
                aria-controls={listId}
                className="h-full min-w-0 flex-1 bg-transparent text-base text-foreground outline-none placeholder:text-fg-subtle md:text-[13px] [&::-webkit-search-cancel-button]:hidden"
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "ArrowDown") moveFocus(event, list.current, field.current);
                }}
                data-testid="project-switcher-search"
              />
              {query ? (
                <button
                  type="button"
                  aria-label={t("clear")}
                  title={t("clear")}
                  onClick={() => {
                    setQuery("");
                    field.current?.focus();
                  }}
                  className="relative inline-grid size-6 shrink-0 cursor-pointer place-items-center rounded-xs text-fg-subtle transition-colors after:absolute after:-inset-2.5 hover:bg-accent hover:text-foreground md:after:hidden"
                >
                  <X className="size-3.5" aria-hidden="true" />
                </button>
              ) : null}
            </div>
            <p className="sr-only" aria-live="polite" data-testid="project-switcher-count">
              {projects ? t("results", { count: shown.length }) : null}
            </p>
            <nav aria-label={t("label")} className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
              {isError ? (
                <p className="flex items-center gap-2 px-2 py-3 text-[13px] text-danger">
                  <TriangleAlert className="size-4 shrink-0" aria-hidden="true" />
                  {t("loadError")}
                </p>
              ) : projects && projects.length === 0 ? (
                <p className="px-2 py-3 text-[13px] text-muted-foreground">{t("none")}</p>
              ) : projects && shown.length === 0 ? (
                <p className="px-2 py-3 text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="project-switcher-no-match">
                  {t("noMatch", { query: query.trim() })}
                </p>
              ) : (
                <ul
                  ref={list}
                  id={listId}
                  className="flex flex-col gap-px"
                  onKeyDown={(event) => moveFocus(event, list.current, field.current)}
                >
                  {shown.map((project) => (
                    <ProjectOption key={project.name} project={project} current={project.name === current} onPick={close} />
                  ))}
                </ul>
              )}
            </nav>
            <div className="shrink-0 border-t pt-1">
              <Link
                href="/"
                onClick={close}
                className="flex min-h-8 items-center gap-2 rounded-sm px-2 text-[13px] font-medium text-muted-foreground outline-none transition-colors hover:bg-accent hover:text-foreground focus-visible:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset max-md:min-h-11"
                data-testid="project-switcher-home"
              >
                <LayoutDashboard className="size-4 shrink-0" aria-hidden="true" />
                {t("home")}
              </Link>
            </div>
          </PopoverContent>
        </Popover>
      </SidebarMenuItem>
    </SidebarMenu>
  );
}
