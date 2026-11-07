"use client";

import { ChevronLeft, Info, Workflow } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { systemQuery, systemsHref } from "@/lib/systems-queries";

import { DESIGNER_FRAME } from "./designer-frame";
import { SystemDesigner } from "./designer-lazy";
import type { DesignerSlots } from "./slots";

type Props = { project: string; slug: string; initialError: ApiErrorInfo | null; slots: DesignerSlots };

/**
 * One agent system of a project (web/DESIGN.md, Agent systems): its name, revision and entry agent over the designer.
 * Every member reads it and may test it; an admin of the project changes it.
 */
export function SystemPage({ project, slug, initialError, slots }: Props) {
  const t = useTranslations("systems");
  const state = useHubQuery(systemQuery(browserApi, project, slug), initialError);
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
              title={system.name || system.slug}
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
            <SystemDesigner project={project} system={system} slots={slots} />
          </>
        )}
      </QueryView>
    </div>
  );
}
