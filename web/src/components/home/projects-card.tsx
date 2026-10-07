"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";

import { NAME_LINK } from "@/components/data/identifier";
import { projectHref } from "@/components/shell/nav";
import { roleLabelKey } from "@/components/shell/role-badge";
import { cn, initials } from "@/lib/utils";

import type { OverviewProject } from "./model";
import { CountPill, HomeCard } from "./parts";

/** Home's Projects card: each project of the visitor's grants, their role, its active plans and repos, its open decisions. */

function ProjectRow({ project }: { project: OverviewProject }) {
  const t = useTranslations("home.projects");
  const tSwitcher = useTranslations("projectSwitcher");
  const tRoles = useTranslations("roles");
  const facts = [
    tRoles(roleLabelKey(project.role)),
    tSwitcher("plans", { count: project.active_plans }),
    tSwitcher("repos", { count: project.repos }),
  ].join(", ");
  return (
    <li className="grid grid-cols-[24px_minmax(0,1fr)_auto] items-center gap-3 border-t px-4 py-3 transition-colors first:border-t-0 hover:bg-accent" data-testid="home-project" data-project={project.name}>
      <span aria-hidden="true" className="grid size-6 place-items-center rounded-xs bg-foreground text-[11px] font-semibold text-background">
        {initials(project.name)}
      </span>
      <div className="flex min-w-0 flex-col gap-0.5">
        <Link href={projectHref(project.name)} className={cn(NAME_LINK, "truncate text-sm")} title={project.name} aria-label={t("open", { name: project.name })}>
          {project.name}
        </Link>
        <span className="text-xs leading-4 text-fg-subtle [overflow-wrap:anywhere]" data-testid="home-project-facts">
          {facts}
        </span>
      </div>
      {project.open_decisions > 0 ? (
        <span data-testid="home-project-decisions" data-count={project.open_decisions}>
          <CountPill value={project.open_decisions} tone="attention" />
          <span className="sr-only">{tSwitcher("decisions", { count: project.open_decisions })}</span>
        </span>
      ) : (
        <span />
      )}
    </li>
  );
}

export function Projects({ projects }: { projects: OverviewProject[] }) {
  const t = useTranslations("home.projects");
  return (
    <HomeCard title={t("title")} count={{ value: projects.length, tone: "neutral", words: t("count", { count: projects.length }) }} testId="home-projects">
      <ul>
        {projects.map((project) => (
          <ProjectRow key={project.name} project={project} />
        ))}
      </ul>
    </HomeCard>
  );
}
