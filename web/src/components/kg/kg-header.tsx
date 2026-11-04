"use client";

import { Eye, ListChecks, Network, Upload, Waypoints } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Tag } from "@/components/data/identifier";
import { TabNav } from "@/components/data/tab-nav";
import { PageHeader } from "@/components/shell/page-header";
import { buildsHref, explorerHref, reviewHref, uploadHref } from "@/lib/kg/routes";

export type KgTab = "explore" | "builds" | "review" | "upload";

/**
 * The head of the knowledge graph's pages and their tabs: the explorer (the graph itself, where the sidebar's entry
 * lands), the builds that made it, the review queue, and Upload data, where an archive is sent as a source of the
 * graph. The graph is read-only on these pages, but for the builds page's Optimize button (`actions`), which asks the
 * hub for a KG optimize run, and an upload, which goes through a build like any source.
 */
export function KgHeader({ project, current, actions }: { project: string; current: KgTab; actions?: ReactNode }) {
  const t = useTranslations("kg");
  // Upload data writes a source of the graph: the read-only tag would say the opposite there.
  const readOnly = current !== "upload";
  const tabs = [
    { id: "explore", label: t("tabs.explore"), href: explorerHref(project, { kind: "overview" }), icon: Waypoints },
    { id: "builds", label: t("tabs.builds"), href: buildsHref(project), icon: Network },
    { id: "review", label: t("tabs.review"), href: reviewHref(project), icon: ListChecks },
    { id: "upload", label: t("tabs.upload"), href: uploadHref(project), icon: Upload },
  ];
  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title={t("title")}
        tags={
          readOnly ? (
            <Tag data-testid="kg-read-only">
              <Eye aria-hidden="true" />
              {t("readOnly")}
            </Tag>
          ) : null
        }
        actions={actions}
        testId="kg-header"
      />
      <TabNav label={t("tabs.label")} tabs={tabs} current={current} testId="kg-tabs" tabTestId="kg-tab" />
    </div>
  );
}
