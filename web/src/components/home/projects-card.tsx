"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";

import { curatorHref, proposalsWaitingHref } from "@/components/curator/queries";
import { NAME_LINK } from "@/components/data/identifier";
import { projectHref } from "@/components/shell/nav";
import { roleLabelKey } from "@/components/shell/role-badge";
import { STATUS_LOOKS, TONE_TEXT, useStatusText } from "@/components/status/status-badge";
import { cn, initials } from "@/lib/utils";

import type { OverviewProject } from "./model";
import { CountPill, HomeCard } from "./parts";

/**
 * Home's Projects card: each project of the visitor's grants, their role, its active plans and repos, its open
 * decisions, and where its Curator stands: running, on duty, idle or paused, and the proposals that wait for an answer.
 */

/** The Curator of a project in one line: its state's icon and word, then what waits, each a link to its page. */
function CuratorLine({ project }: { project: OverviewProject }) {
  const t = useTranslations("home.projects");
  const text = useStatusText("curator");
  const curator = project.curator;
  if (!curator) return null;
  const look = STATUS_LOOKS.curator[curator.state];
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-xs leading-4" data-testid="home-project-curator" data-state={curator.state}>
      <Link
        href={curatorHref(project.name)}
        className={cn("inline-flex min-w-0 items-center gap-1 underline decoration-current/40 underline-offset-4 hover:decoration-current", TONE_TEXT[look.tone])}
        data-testid="home-project-curator-link"
      >
        {look.dot === "live" ? (
          <span className="relative inline-flex size-2 shrink-0 rounded-full bg-current" aria-hidden="true">
            <span className="absolute inset-0 animate-live-ping rounded-full bg-current" />
          </span>
        ) : (
          <look.icon className="size-3.5 shrink-0" aria-hidden="true" />
        )}
        {t("curator", { state: text(curator.state) })}
      </Link>
      {curator.open_proposals > 0 ? (
        <Link
          href={proposalsWaitingHref(project.name)}
          className="font-medium text-attention underline decoration-current/40 underline-offset-4 hover:decoration-current"
          data-testid="home-project-proposals"
          data-count={curator.open_proposals}
        >
          {t("proposals", { count: curator.open_proposals })}
        </Link>
      ) : null}
    </span>
  );
}

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
        <CuratorLine project={project} />
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
