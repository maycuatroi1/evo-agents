"use client";

import { useQuery } from "@tanstack/react-query";
import { BadgeCheck, Cpu, Puzzle, ShieldAlert, ShieldCheck } from "lucide-react";
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
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { type Plugin, type PluginPublisher } from "@/lib/plugin-queries";

import { adminPluginHref, pluginsQuery, publishersQuery } from "./queries";
import { StoreStateBadge, TierTag, UpgradeTag } from "./parts";
import { isFromImage, StoreActions } from "./store-actions";

const NARROW_HIDDEN = { publisher: "hidden lg:table-cell", tier: "hidden md:table-cell", version: "hidden sm:table-cell" };

/** A publisher of the store: trusted now (in EVO_HUB_PLUGIN_PUBLISHERS), or no longer, though its versions stay. */
function PublisherCell({ publisher, trusted }: { publisher: string; trusted: boolean | null }) {
  const t = useTranslations("plugins.admin");
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5">
      {trusted === false ? (
        <ShieldAlert className="size-4 shrink-0 text-attention" aria-hidden="true" />
      ) : (
        <ShieldCheck className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      )}
      <span className="truncate font-mono text-xs">{publisher}</span>
      {trusted === false ? <span className="sr-only">, {t("untrusted")}</span> : null}
    </span>
  );
}

function VersionCell({ plugin }: { plugin: Plugin }) {
  const t = useTranslations("plugins.admin");
  if (plugin.installed_version === null) {
    return <span className="text-xs text-fg-subtle">{t("latest", { version: plugin.latest_version })}</span>;
  }
  return (
    <span className="inline-flex flex-wrap items-center justify-end gap-1.5">
      <span className="font-mono text-xs text-foreground tabular-nums">{plugin.installed_version}</span>
      {plugin.upgrade_to ? <UpgradeTag version={plugin.upgrade_to} /> : null}
    </span>
  );
}

function PluginsTable({ plugins, trusted }: { plugins: Plugin[]; trusted: (publisher: string) => boolean | null }) {
  const t = useTranslations("plugins.admin");
  const columns = useMemo(() => {
    const helper = dataTableColumns<Plugin>();
    return helper.columns([
      helper.accessor("title", {
        header: () => t("columns.plugin"),
        sortFn: "alphanumeric",
        meta: { primary: true },
        cell: (info) => {
          const plugin = info.row.original;
          const sub = plugin.description ? `${plugin.name}, ${plugin.description}` : plugin.name;
          return (
            <CellMain sub={sub} subTitle={sub}>
              <Link href={adminPluginHref(plugin.name)} className={`${NAME_LINK} truncate`} data-plugin-link={plugin.name}>
                {plugin.title}
              </Link>
            </CellMain>
          );
        },
      }),
      helper.accessor("publisher", {
        header: () => t("columns.publisher"),
        sortFn: "alphanumeric",
        cell: (info) => (
          // A core plugin of the hub's image is signed by nobody: the image is what the operator trusts.
          <PublisherCell publisher={info.getValue()} trusted={isFromImage(info.row.original) ? null : trusted(info.getValue())} />
        ),
      }),
      helper.accessor("tier", {
        header: () => t("columns.tier"),
        cell: (info) => <TierTag tier={info.getValue()} />,
      }),
      helper.display({
        id: "version",
        header: () => t("columns.version"),
        meta: { numeric: true },
        cell: (info) => <VersionCell plugin={info.row.original} />,
      }),
      helper.accessor("state", {
        header: () => t("columns.state"),
        cell: (info) => <StoreStateBadge state={info.getValue()} />,
      }),
      helper.display({
        id: "actions",
        header: () => <span className="sr-only">{t("columns.actions")}</span>,
        meta: { actions: true },
        cell: (info) => (
          <div className="flex justify-end gap-1.5">
            <StoreActions plugin={info.row.original} fromImage={isFromImage(info.row.original)} size="sm" />
          </div>
        ),
      }),
    ]);
  }, [t, trusted]);

  return (
    <DataTable
      data={plugins}
      columns={columns}
      caption={t("caption")}
      getRowId={(row) => row.name}
      columnClassNames={NARROW_HIDDEN}
      testId="plugins-table"
      mobile={(plugin) => ({
        title: plugin.title,
        titleText: plugin.title,
        href: adminPluginHref(plugin.name),
        status: <StoreStateBadge state={plugin.state} />,
        tags: plugin.upgrade_to ? <UpgradeTag version={plugin.upgrade_to} /> : null,
        meta: `${plugin.publisher}, ${plugin.installed_version ?? t("latest", { version: plugin.latest_version })}`,
        data: { "plugin-name": plugin.name },
      })}
    />
  );
}

/** The publishers the operator trusts, with what signs for each, and which may publish core plugins. */
function PublishersCard({ publishers }: { publishers: PluginPublisher[] | undefined }) {
  const t = useTranslations("plugins.admin.publishers");
  return (
    <Card className="min-w-0 self-start" data-testid="plugin-publishers">
      <CardHeader>
        <CardTitle>
          <h2 className="flex items-center gap-2">
            <BadgeCheck className="size-4 text-muted-foreground" aria-hidden="true" />
            {t("title")}
          </h2>
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent className="min-w-0">
        {publishers === undefined ? null : publishers.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("none")}</p>
        ) : (
          <ul className="flex flex-col divide-y">
            {publishers.map((publisher) => (
              <li key={publisher.id} className="flex flex-col gap-1 py-2.5 first:pt-0 last:pb-0" data-publisher={publisher.id}>
                <span className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-[13px] font-medium text-foreground">{publisher.id}</span>
                  {publisher.core ? (
                    <Tag title={t("coreHint")}>
                      <Cpu aria-hidden="true" />
                      {t("core")}
                      <span className="sr-only">: {t("coreHint")}</span>
                    </Tag>
                  ) : null}
                </span>
                {publisher.identities.map((identity) => (
                  <code key={identity} className="font-mono text-xs break-all text-fg-subtle">
                    {identity}
                  </code>
                ))}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

/**
 * /admin/plugins, for hub admins (web/DESIGN.md, Plugins): every plugin of the hub's store with its publisher (flagged
 * once the operator no longer trusts it), its tier, the version installed and an upgrade waiting, and its state; Install
 * and Upgrade on its row, the rest on its page. Beside the list, the publishers the operator trusts. A plugin is
 * published with the CLI, which the empty state says. Anyone else gets the API's 403.
 */
export function AdminPluginsPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("plugins.admin");
  const state = useHubQuery(pluginsQuery(browserApi), initialError);
  const { data: publishers } = useQuery(publishersQuery(browserApi));
  const trusted = useMemo(() => {
    const ids = publishers ? new Set(publishers.map((item) => item.id)) : null;
    return (publisher: string) => (ids ? ids.has(publisher) : null);
  }, [publishers]);
  const plugins = state.status === "success" ? state.data : null;
  return (
    <div className="flex flex-col gap-6" data-testid="admin-plugins">
      <PageHeader
        title={t("title")}
        tags={plugins ? <Badge variant="secondary">{t("count", { count: plugins.length })}</Badge> : null}
      />
      <div className="grid min-w-0 gap-6 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <QueryView state={state} loading={<TableSkeleton rows={3} />}>
          {(list) =>
            list.length === 0 ? (
              <EmptyState icon={Puzzle} title={t("emptyTitle")} description={t("emptyDescription")} testId="plugins-empty">
                <code className="rounded-xs bg-surface-sunken px-2 py-1 font-mono text-xs">{t("publishCommand")}</code>
              </EmptyState>
            ) : (
              <section aria-labelledby="plugins-list-title" className="min-w-0">
                <h2 id="plugins-list-title" className="sr-only">
                  {t("caption")}
                </h2>
                <DataCard testId="plugins-card">
                  <PluginsTable plugins={list} trusted={trusted} />
                </DataCard>
              </section>
            )
          }
        </QueryView>
        <PublishersCard publishers={publishers} />
      </div>
    </div>
  );
}
