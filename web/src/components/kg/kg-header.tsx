"use client";

import { Eye, ListChecks, Network, Waypoints } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Tag } from "@/components/data/identifier";
import { TabNav } from "@/components/data/tab-nav";
import { PageHeader } from "@/components/shell/page-header";
import { buildsHref, explorerHref, reviewHref } from "@/lib/kg/routes";

export type KgTab = "explore" | "builds" | "review";

/**
 * The head of the knowledge graph's pages and their tabs: the explorer (the graph itself, where the sidebar's entry
 * lands), the builds that made it, and the review queue. Read-only: nothing on these pages queues a build.
 */
export function KgHeader({ project, current, actions }: { project: string; current: KgTab; actions?: ReactNode }) {
  const t = useTranslations("kg");
  const tabs = [
    { id: "explore", label: t("tabs.explore"), href: explorerHref(project, { kind: "overview" }), icon: Waypoints },
    { id: "builds", label: t("tabs.builds"), href: buildsHref(project), icon: Network },
    { id: "review", label: t("tabs.review"), href: reviewHref(project), icon: ListChecks },
  ];
  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title={t("title")}
        tags={
          <Tag data-testid="kg-read-only">
            <Eye aria-hidden="true" />
            {t("readOnly")}
          </Tag>
        }
        actions={actions}
        testId="kg-header"
      />
      <TabNav label={t("tabs.label")} tabs={tabs} current={current} testId="kg-tabs" tabTestId="kg-tab" />
    </div>
  );
}
