"use client";

import { CircleCheck, CircleX, History } from "lucide-react";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { DataCard } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { Tag } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import type { PluginDetail, PluginVersion } from "@/lib/plugin-queries";

import { StoreActions } from "./store-actions";

/**
 * Every version of a plugin in the store, the newest first (web/DESIGN.md, Plugins): the version, whether it runs on
 * this hub's release (the hub's reason when it does not), who signed it and how, who published it and when. While none
 * is installed, a compatible version can be installed from its row.
 */
export function VersionsTable({ plugin, fromImage }: { plugin: PluginDetail; fromImage: boolean }) {
  const t = useTranslations("plugins.versions");
  const columns = useMemo(() => {
    const helper = dataTableColumns<PluginVersion>();
    return helper.columns([
      helper.accessor("version", {
        header: () => t("columns.version"),
        meta: { primary: true },
        cell: (info) => {
          const version = info.row.original;
          return (
            <CellMain sub={version.incompatible_reason ?? undefined} subTitle={version.incompatible_reason ?? undefined} danger={!version.compatible}>
              <span className="font-mono tabular-nums">{version.version}</span>
              {version.version === plugin.installed_version ? <Tag>{t("installed")}</Tag> : null}
            </CellMain>
          );
        },
      }),
      helper.accessor("compatible", {
        header: () => t("columns.runs"),
        cell: (info) =>
          info.getValue() ? (
            <span className="inline-flex items-center gap-1.5 text-success">
              <CircleCheck className="size-4" aria-hidden="true" />
              {t("runsHere")}
            </span>
          ) : (
            <span className="inline-flex items-center gap-1.5 text-danger">
              <CircleX className="size-4" aria-hidden="true" />
              {t("doesNotRun")}
            </span>
          ),
      }),
      helper.accessor("signed_by", {
        header: () => t("columns.signedBy"),
        cell: (info) => (
          <span className="flex min-w-0 flex-col gap-px">
            <span className="text-xs text-foreground">{t(`kinds.${info.row.original.signature_kind}`)}</span>
            <code className="truncate font-mono text-xs text-fg-subtle" title={info.getValue()}>
              {info.getValue()}
            </code>
          </span>
        ),
      }),
      helper.accessor("published_at", {
        header: () => t("columns.published"),
        meta: { numeric: true },
        cell: (info) => (
          <span className="flex flex-col items-end gap-px">
            <When value={info.getValue()} short />
            <span className="text-xs text-fg-subtle">{info.row.original.published_by ?? t("theImage")}</span>
          </span>
        ),
      }),
      helper.display({
        id: "actions",
        header: () => <span className="sr-only">{t("columns.actions")}</span>,
        meta: { actions: true },
        cell: (info) =>
          plugin.state === "available" && info.row.original.compatible ? (
            <div className="flex justify-end">
              <StoreActions plugin={plugin} fromImage={fromImage} size="sm" version={info.row.original.version} />
            </div>
          ) : null,
      }),
    ]);
  }, [t, plugin, fromImage]);

  return (
    <section aria-labelledby="plugin-versions-title" className="flex min-w-0 flex-col gap-3" data-testid="plugin-versions">
      <h2 id="plugin-versions-title" className="flex items-center gap-2 text-[15px] leading-[22px] font-semibold">
        <History className="size-4 text-muted-foreground" aria-hidden="true" />
        {t("title")}
      </h2>
      <DataCard>
        <DataTable
          data={plugin.versions}
          columns={columns}
          caption={t("caption", { name: plugin.title })}
          getRowId={(row) => row.version}
          columnClassNames={{ signed_by: "hidden md:table-cell", published_at: "hidden sm:table-cell", compatible: "hidden sm:table-cell" }}
          testId="plugin-versions-table"
        />
      </DataCard>
    </section>
  );
}
