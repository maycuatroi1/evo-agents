"use client";

import { Info, Puzzle } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { DataCard } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK, Tag } from "@/components/data/identifier";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { pendingPermissions, pluginOn } from "@/lib/plugin-pages";
import { pluginPageHref, type ProjectPlugin, projectPluginHref, projectPluginsQuery } from "@/lib/plugin-queries";

import { useProjectAdmin } from "./data";
import { ProjectStateBadge, TierTag } from "./parts";
import { ProjectActions } from "./project-actions";

const NARROW_HIDDEN = { tier: "hidden md:table-cell", version: "hidden sm:table-cell", pages: "hidden lg:table-cell" };

/** The state, and how many permissions an upgrade added that the project has not granted yet. */
function StateCell({ plugin }: { plugin: ProjectPlugin }) {
  const t = useTranslations("plugins.project");
  const pending = pendingPermissions(plugin);
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <ProjectStateBadge plugin={plugin} />
      {pending.length > 0 ? (
        <Tag className="bg-attention-soft text-attention" data-testid="plugin-pending">
          {t("pending", { count: pending.length })}
        </Tag>
      ) : null}
    </span>
  );
}

function PagesCell({ project, plugin }: { project: string; plugin: ProjectPlugin }) {
  const t = useTranslations("plugins.project");
  if (plugin.pages.length === 0) return <span className="text-xs text-fg-subtle">{t("noPages")}</span>;
  if (!pluginOn(plugin)) return <span className="text-xs text-fg-subtle">{plugin.pages.map((page) => page.title).join(", ")}</span>;
  return (
    <span className="flex flex-wrap gap-x-3 gap-y-1">
      {plugin.pages.map((page) => (
        <Link key={page.name} href={pluginPageHref(project, plugin.name, page.name)} className="text-[13px] text-brand hover:text-brand-hover">
          {page.title}
        </Link>
      ))}
    </span>
  );
}

function PluginsTable({ project, plugins, admin }: { project: string; plugins: ProjectPlugin[]; admin: boolean }) {
  const t = useTranslations("plugins.project");
  const columns = useMemo(() => {
    const helper = dataTableColumns<ProjectPlugin>();
    return helper.columns([
      helper.accessor("title", {
        header: () => t("columns.plugin"),
        sortFn: "alphanumeric",
        meta: { primary: true },
        cell: (info) => {
          const plugin = info.row.original;
          const sub = plugin.description || plugin.publisher;
          return (
            <CellMain sub={sub} subTitle={sub}>
              <Link href={projectPluginHref(project, plugin.name)} className={`${NAME_LINK} truncate`} data-plugin-link={plugin.name}>
                {plugin.title}
              </Link>
            </CellMain>
          );
        },
      }),
      helper.accessor("tier", { header: () => t("columns.tier"), cell: (info) => <TierTag tier={info.getValue()} /> }),
      helper.accessor("version", {
        header: () => t("columns.version"),
        cell: (info) => <span className="font-mono text-xs tabular-nums">{info.getValue()}</span>,
      }),
      helper.display({ id: "pages", header: () => t("columns.pages"), cell: (info) => <PagesCell project={project} plugin={info.row.original} /> }),
      helper.display({ id: "state", header: () => t("columns.state"), cell: (info) => <StateCell plugin={info.row.original} /> }),
      ...(admin
        ? [
            helper.display({
              id: "actions",
              header: () => <span className="sr-only">{t("columns.actions")}</span>,
              meta: { actions: true },
              cell: (info) => (
                <div className="flex justify-end gap-1.5">
                  <ProjectActions project={project} plugin={info.row.original} size="sm" />
                </div>
              ),
            }),
          ]
        : []),
    ]);
  }, [t, project, admin]);

  return (
    <DataTable
      data={plugins}
      columns={columns}
      caption={t("caption", { project })}
      getRowId={(row) => row.name}
      columnClassNames={NARROW_HIDDEN}
      testId="project-plugins-table"
      mobile={(plugin) => ({
        title: plugin.title,
        titleText: plugin.title,
        href: projectPluginHref(project, plugin.name),
        status: <ProjectStateBadge plugin={plugin} />,
        meta: `${plugin.publisher}, ${plugin.version}`,
        data: { "plugin-name": plugin.name },
      })}
    />
  );
}

/**
 * A project's plugins (/p/{project}/plugins; web/DESIGN.md, Plugins): every plugin installed on the hub, with its tier,
 * version, the pages it adds and whether the project enabled it. Every member reads; an admin of the project enables
 * a plugin through its consent, grants what an upgrade added, and disables it, from its row or its page. A hub admin
 * installs plugins (Administration, Plugins), which the empty state says.
 */
export function ProjectPluginsPage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("plugins.project");
  const state = useHubQuery(projectPluginsQuery(browserApi, project), initialError);
  const admin = useProjectAdmin(project);
  const plugins = state.status === "success" ? state.data : null;
  const enabled = (plugins ?? []).filter((plugin) => plugin.enabled).length;
  return (
    <div className="flex flex-col gap-6" data-testid="project-plugins">
      <PageHeader
        title={t("title")}
        tags={plugins && plugins.length > 0 ? <Badge variant="secondary">{t("enabledCount", { count: enabled, total: plugins.length })}</Badge> : null}
      />
      {admin ? null : (
        <p className="flex items-start gap-2 rounded-md bg-surface-sunken px-3 py-2.5 text-[13px] text-muted-foreground" role="note" data-testid="plugins-read-only">
          <Info className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          {t("readOnly")}
        </p>
      )}
      <QueryView state={state} loading={<TableSkeleton rows={2} />}>
        {(list) =>
          list.length === 0 ? (
            <EmptyState icon={Puzzle} title={t("emptyTitle")} description={t("emptyDescription")} testId="project-plugins-empty" />
          ) : (
            <section aria-labelledby="project-plugins-title">
              <h2 id="project-plugins-title" className="sr-only">
                {t("caption", { project })}
              </h2>
              <DataCard testId="project-plugins-card">
                <PluginsTable project={project} plugins={list} admin={admin} />
              </DataCard>
            </section>
          )
        }
      </QueryView>
    </div>
  );
}
