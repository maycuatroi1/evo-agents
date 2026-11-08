"use client";

import { Activity, ChevronLeft, Info, PencilRuler, Workflow } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { TabNav } from "@/components/data/tab-nav";

import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { systemHref, systemObserveHref, systemQuery, systemsHref } from "@/lib/systems-queries";

import { DESIGNER_FRAME } from "./designer-frame";
import { SystemDesigner, SystemObserver } from "./designer-lazy";
import type { DesignerSlots } from "./slots";
import { SystemName } from "./system-name";

export type SystemMode = "design" | "observe";
type Props = { project: string; slug: string; mode: SystemMode; initialError: ApiErrorInfo | null; slots: DesignerSlots };

/**
 * One agent system of a project (web/DESIGN.md, Agent systems): its name, revision and entry agent over the designer.
 * Every member reads it and may test it; an admin of the project changes it, and renames it from the title. Two tabs: Design (the designer) and
 * Observe (`?mode=observe`, the saved revision with the work going through it). The designer, once opened, stays
 * mounted under Observe, so a draft survives a look at the traces.
 */
export function SystemPage({ project, slug, mode, initialError, slots }: Props) {
  const t = useTranslations("systems");
  const state = useHubQuery(systemQuery(browserApi, project, slug), initialError);
  const [designed, setDesigned] = useState(mode === "design");
  if (mode === "design" && !designed) setDesigned(true);
  const tabs = [
    { id: "design", label: t("modes.design"), href: systemHref(project, slug), icon: PencilRuler },
    { id: "observe", label: t("modes.observe"), href: systemObserveHref(project, slug), icon: Activity },
  ];
  return (
    <div className="flex flex-col gap-4" data-testid="system-page" data-slug={slug}>
      <Link href={systemsHref(project)} className="inline-flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ChevronLeft className="size-4" aria-hidden="true" />
        {t("title")}
      </Link>
      <QueryView state={state} loading={<Skeleton className={DESIGNER_FRAME} />}>
        {(system) => (
          <>
            <PageHeader
              title={<SystemName project={project} system={system} />}
              leading={<Workflow className="size-5 text-muted-foreground" aria-hidden="true" />}
              tags={
                <>
                  <Badge variant="secondary" className="font-mono">
                    {system.slug}
                  </Badge>
                  <Badge variant="outline" data-testid="system-revision">
                    {t("revision", { revision: system.revision })}
                  </Badge>
                </>
              }
              sub={system.entry_agent ? t("entryAgent", { handle: system.entry_agent }) : t("noEntryAgent")}
            />
            {system.can_change ? null : (
              <p role="note" className="flex items-start gap-2.5 rounded-md border bg-surface-sunken px-4 py-3 text-[13px] text-pretty text-muted-foreground" data-testid="system-read-only">
                <Info className="mt-px size-4 shrink-0" aria-hidden="true" />
                <span>{t("readOnly", { project })}</span>
              </p>
            )}
            <TabNav label={t("modes.label")} tabs={tabs} current={mode} testId="system-modes" tabTestId="system-mode" />
            {designed ? (
              <div hidden={mode !== "design"}>
                <SystemDesigner project={project} system={system} slots={slots} />
              </div>
            ) : null}
            {mode === "observe" ? <SystemObserver project={project} system={system} /> : null}
          </>
        )}
      </QueryView>
    </div>
  );
}
