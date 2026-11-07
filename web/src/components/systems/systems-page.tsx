"use client";

import { Plus, Workflow } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { DataCard } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { whoamiQuery } from "@/lib/queries";
import type { SystemSummary } from "@/lib/systems";
import { systemHref, systemsQuery } from "@/lib/systems-queries";
import { useQuery } from "@tanstack/react-query";

import { NewSystemDialog } from "./new-system-dialog";

const NARROW_HIDDEN = { size: "hidden md:table-cell", updated: "hidden lg:table-cell" };

function SystemsTable({ project, systems }: { project: string; systems: SystemSummary[] }) {
  const t = useTranslations("systems.list");
  const columns = useMemo(() => {
    const helper = dataTableColumns<SystemSummary>();
    return helper.columns([
      helper.accessor("name", {
        header: () => t("columns.system"),
        sortFn: "alphanumeric",
        meta: { primary: true },
        cell: (info) => {
          const system = info.row.original;
          return (
            <CellMain sub={system.slug} subTitle={system.slug}>
              <Link href={systemHref(project, system.slug)} className={`${NAME_LINK} truncate`} data-system-link={system.slug}>
                {system.name || system.slug}
              </Link>
            </CellMain>
          );
        },
      }),
      helper.accessor((row) => row.entry_agent ?? "", {
        id: "entry",
        header: () => t("columns.entry"),
        sortFn: "alphanumeric",
        cell: (info) => (info.getValue() ? <span className="font-mono text-xs">@{info.getValue()}</span> : <span className="text-muted-foreground">{t("noEntry")}</span>),
      }),
      helper.display({
        id: "size",
        header: () => t("columns.size"),
        cell: (info) => <span className="text-xs tabular-nums">{t("size", { nodes: info.row.original.nodes, edges: info.row.original.edges })}</span>,
      }),
      helper.accessor("revision", {
        header: () => t("columns.revision"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => <span className="font-mono text-xs tabular-nums">{t("revision", { revision: info.getValue() })}</span>,
      }),
      helper.accessor((row) => new Date(row.updated_at), {
        id: "updated",
        header: () => t("columns.updated"),
        sortFn: "datetime",
        meta: { numeric: true },
        cell: (info) => <When value={info.row.original.updated_at} short />,
      }),
    ]);
  }, [t, project]);
  return (
    <DataTable
      data={systems}
      columns={columns}
      caption={t("caption", { project })}
      getRowId={(row) => row.slug}
      columnClassNames={NARROW_HIDDEN}
      testId="systems-table"
      mobile={(system) => ({
        title: system.name || system.slug,
        titleText: system.name || system.slug,
        href: systemHref(project, system.slug),
        meta: <span className="font-mono">{system.slug}</span>,
        metaText: system.slug,
        data: { "system-slug": system.slug },
      })}
    />
  );
}

/**
 * A project's agent systems (web/DESIGN.md, Agent systems): agents, graphs, a curator and an orchestrator joined on a
 * canvas, each system with its entry agent, its size and its revision. Every member reads them; an admin of the
 * project creates one here and lands in its designer.
 */
export function SystemsPage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("systems");
  const router = useRouter();
  const state = useHubQuery(systemsQuery(browserApi, project), initialError);
  const me = useQuery(whoamiQuery(browserApi));
  const admin = me.data?.grants.some((grant) => grant.project === project && grant.role === "admin") ?? false;
  const [creating, setCreating] = useState(false);
  const systems = state.status === "success" ? state.data : null;
  const newButton = admin ? (
    <Button onClick={() => setCreating(true)} data-testid="systems-new">
      <Plus aria-hidden="true" />
      {t("newButton")}
    </Button>
  ) : null;
  return (
    <div className="flex flex-col gap-6" data-testid="systems-page">
      <PageHeader
        title={t("title")}
        tags={systems ? <Badge variant="secondary">{t("count", { count: systems.length })}</Badge> : null}
        sub={t("sub")}
        actions={newButton}
      />
      <QueryView state={state} loading={<TableSkeleton rows={3} />}>
        {(rows) =>
          rows.length ? (
            <section aria-labelledby="systems-list-title">
              <h2 id="systems-list-title" className="sr-only">
                {t("list.caption", { project })}
              </h2>
              <DataCard testId="systems-card">
                <SystemsTable project={project} systems={rows} />
              </DataCard>
            </section>
          ) : (
            <EmptyState icon={Workflow} title={t("emptyTitle")} description={admin ? t("emptyAdmin") : t("emptyReader")} testId="systems-empty">
              {newButton}
            </EmptyState>
          )
        }
      </QueryView>
      {admin ? <NewSystemDialog project={project} open={creating} onOpenChange={setCreating} onCreated={(slug) => router.push(systemHref(project, slug))} /> : null}
    </div>
  );
}
