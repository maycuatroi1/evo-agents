"use client";

import { Puzzle } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { NotFoundState, PageSkeleton } from "@/components/states/states";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { shownPage } from "@/lib/plugin-pages";
import { projectPluginHref, projectPluginQuery } from "@/lib/plugin-queries";

import { SandboxTag } from "./parts";
import { PluginFrame } from "./plugin-frame";

/**
 * An isolated plugin's page in a project (/p/{project}/x/{plugin}/{page}, and any sub path after it; web/DESIGN.md,
 * Plugins): the page's title as the h1, the plugin as a tag that opens its settings in the project, Sandboxed, one line
 * saying what the page may read (the project's grant), then the frame. A native page of a core plugin never comes
 * here: it has a route of its own under x/<plugin>.
 */
export function PluginPageView({
  project,
  plugin: name,
  page: pageName,
  path = "",
  initialError,
}: {
  project: string;
  plugin: string;
  page: string;
  /** The sub path after the page's name, "" for none. */
  path?: string;
  initialError: ApiErrorInfo | null;
}) {
  const t = useTranslations("plugins.page");
  const state = useHubQuery(projectPluginQuery(browserApi, project, name), initialError);
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(plugin) => {
        const page = shownPage(plugin, pageName);
        if (page === null || page.native) return <NotFoundState title={t("missingTitle")} description={t("missingDescription")} />;
        const granted = plugin.granted.length ? plugin.granted.join(", ") : t("nothing");
        return (
          <div className="flex flex-col gap-4" data-testid="plugin-page" data-plugin={plugin.name} data-page={page.name}>
            <PageHeader
              title={page.title}
              tags={
                <>
                  <Link href={projectPluginHref(project, plugin.name)} className="rounded-xs">
                    <Tag className="hover:text-foreground">
                      <Puzzle aria-hidden="true" />
                      {t("fromPlugin", { name: plugin.title, version: plugin.version })}
                    </Tag>
                  </Link>
                  <SandboxTag />
                </>
              }
              sub={t("mayRead", { granted })}
            />
            <PluginFrame project={project} plugin={plugin} page={page} path={path} />
          </div>
        );
      }}
    </QueryView>
  );
}
