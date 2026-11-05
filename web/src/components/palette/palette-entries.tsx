"use client";

import { FilePen, FilePlus, FolderKanban, Keyboard, Play, Plus, Send } from "lucide-react";
import type { Route } from "next";
import { useTranslations } from "next-intl";

import { HOME_NAV, HUB_NAV, PROJECT_NAV, projectHref, useNavShown } from "@/components/shell/nav";
import { useCurrentProject } from "@/components/shell/project-switcher";
import { roleLabelKey } from "@/components/shell/role-badge";
import { NAV_SHORTCUTS } from "@/components/shell/shortcuts";
import type { PlanSummary } from "@/lib/plans";

import { canRegister, dispatchProjects, type Grant, IDLE_LIMIT, planRunOffers, reviseOffers, type Scope } from "./model";
import type { Entry } from "./palette-view";

/** A dialog the palette opens once it has closed. Each is the one its page opens; the palette adds no form of its own. */
export type PaletteDialog =
  | { kind: "dispatch"; project: string }
  | { kind: "planRun"; project: string; planId: string }
  | { kind: "author"; project: string; planId: string | null }
  | { kind: "register" };

/** What choosing an item does, but a rerun: open a page, open a dialog, or open the keyboard shortcuts. */
export type OpenTarget = { kind: "page"; href: Route } | { kind: "dialog"; dialog: PaletteDialog } | { kind: "shortcuts" };

/**
 * After the palette has closed: open the dialog chosen, leave focus to the page opened, give focus back, or give it
 * back and open the shortcuts dialog (which gives it back there in turn).
 */
export type After = { kind: "dialog"; dialog: PaletteDialog } | { kind: "navigate" } | { kind: "stay" } | { kind: "shortcuts" };

type Take = <T>(entries: Entry<T>[], cap?: number) => Entry<T>[];

/** The meta at an item's end: the project when the palette looks in every project, then what the item says. */
export function metaOf(project: string | null, text: string | null): string {
  return [project, text].filter(Boolean).join(", ");
}

/** The actions that open a dialog, only those the grants allow, each opening the dialog its page opens. */
export function useDialogActions({
  plansByProject,
  grants,
  scoped,
  scope,
}: {
  plansByProject: { project: string; plans: PlanSummary[] }[];
  grants: readonly Grant[];
  scoped: string[];
  scope: Scope;
}) {
  const t = useTranslations("palette");
  const current = useCurrentProject();
  const showProject = scope === null;
  const runPlans: Entry<OpenTarget>[] = planRunOffers(plansByProject, grants).map((offer) => ({
    value: `action:plan-run:${offer.project}:${offer.planId}`,
    icon: Play,
    label: <span className="truncate">{t("actions.runPlan", { plan: offer.title })}</span>,
    text: `${t("actions.runPlan", { plan: offer.title })} run plan ${offer.planId} ${offer.project}`,
    meta: metaOf(showProject ? offer.project : null, t("actions.stepsLeft", { count: offer.left })),
    target: { kind: "dialog", dialog: { kind: "planRun", project: offer.project, planId: offer.planId } },
  }));
  const newPlans: Entry<OpenTarget>[] = dispatchProjects(scoped, grants).map((project) => {
    const label = scope === null ? t("actions.newPlanIn", { project }) : t("actions.newPlan");
    return {
      value: `action:new-plan:${project}`,
      icon: FilePlus,
      label: <span className="truncate">{label}</span>,
      text: `${label} new plan write author ${project}`,
      target: { kind: "dialog", dialog: { kind: "author", project, planId: null } },
    };
  });
  const revisions: Entry<OpenTarget>[] = reviseOffers(plansByProject, grants).map((offer) => ({
    value: `action:revise:${offer.project}:${offer.planId}`,
    icon: FilePen,
    label: <span className="truncate">{t("actions.revise", { plan: offer.title })}</span>,
    text: `${t("actions.revise", { plan: offer.title })} revise plan with agent author ${offer.planId} ${offer.project}`,
    meta: showProject ? offer.project : undefined,
    target: { kind: "dialog", dialog: { kind: "author", project: offer.project, planId: offer.planId } },
  }));
  const dispatches: Entry<OpenTarget>[] = dispatchProjects(scoped, grants).map((project) => {
    const label = scope === null ? t("actions.dispatchIn", { project }) : t("actions.dispatch");
    return {
      value: `action:dispatch:${project}`,
      icon: Send,
      label: <span className="truncate">{label}</span>,
      text: `${label} dispatch step ${project}`,
      shortcut: project === current ? "dispatch" : undefined,
      target: { kind: "dialog", dialog: { kind: "dispatch", project } },
    };
  });
  const registers: Entry<OpenTarget>[] = canRegister(grants)
    ? [
        {
          value: "action:register",
          icon: Plus,
          label: <span className="truncate">{t("actions.register")}</span>,
          text: `${t("actions.register")} register worker`,
          target: { kind: "dialog", dialog: { kind: "register" } },
        },
      ]
    : [];
  return { runPlans, newPlans, revisions, dispatches, registers };
}

/** Go to: the scope's project pages, the hub's pages, then the projects (while looking in every project, or typing). */
export function useGotoEntries({
  scope,
  typed,
  grants,
  admin,
  take,
  limit,
}: {
  scope: Scope;
  typed: boolean;
  grants: readonly Grant[];
  admin: boolean;
  take: Take;
  limit: number;
}): Entry<OpenTarget>[] {
  const t = useTranslations("palette");
  const tNav = useTranslations("nav");
  const tRoles = useTranslations("roles");
  const current = useCurrentProject();
  const shown = useNavShown(); // a page whose feature a hub admin turned off is not offered
  const projectPages: Entry<OpenTarget>[] =
    scope === null
      ? []
      : PROJECT_NAV.filter((item) => shown(item, scope)).map((item) => {
          const label = t("goto.page", { page: tNav(item.label), project: scope });
          return {
            value: `goto:${scope}:${item.segment || "overview"}`,
            icon: item.icon,
            label: <span className="truncate">{label}</span>,
            text: label,
            // G P and G R go to the plans and runs of the page's project.
            shortcut: scope === current ? NAV_SHORTCUTS[item.label] : undefined,
            target: { kind: "page", href: projectHref(scope, item.segment) },
          };
        });
  const hubPages: Entry<OpenTarget>[] = [...HOME_NAV, ...HUB_NAV]
    .filter((item) => (!item.adminOnly || admin) && shown(item))
    .map((item) => ({
      value: `goto:hub:${item.label}`,
      icon: item.icon,
      label: <span className="truncate">{tNav(item.label)}</span>,
      text: tNav(item.label),
      shortcut: NAV_SHORTCUTS[item.label],
      target: { kind: "page", href: item.href },
    }));
  const projectLinks: Entry<OpenTarget>[] =
    scope === null || typed
      ? grants
          .filter((grant) => grant.project !== scope)
          .sort((a, b) => a.project.localeCompare(b.project))
          .slice(0, typed ? undefined : IDLE_LIMIT)
          .map((grant) => ({
            value: `goto:project:${grant.project}`,
            icon: FolderKanban,
            label: <span className="truncate">{grant.project}</span>,
            text: grant.project,
            meta: tRoles(roleLabelKey(grant.role)),
            target: { kind: "page", href: projectHref(grant.project) },
          }))
      : [];
  return take([...projectPages, ...hubPages, ...projectLinks], limit);
}

/** Help: the shortcuts dialog, once what is typed names it. `offered`: the shell can open it and something is typed. */
export function useHelpEntries(offered: boolean, take: Take): Entry<OpenTarget>[] {
  const t = useTranslations("palette");
  return offered
    ? take([
        {
          value: "help:shortcuts",
          icon: Keyboard,
          label: <span className="truncate">{t("shortcuts")}</span>,
          text: `${t("shortcuts")} keyboard shortcuts keys help`,
          shortcut: "help",
          target: { kind: "shortcuts" },
        },
      ])
    : [];
}
