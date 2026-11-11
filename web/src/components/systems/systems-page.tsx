"use client";

import { Plus, Workflow } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback, useMemo } from "react";

import { DataCard } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";
import { useWriteFailure } from "@/components/feedback/notice";
import { notifyFailure } from "@/components/feedback/toast";
import { When } from "@/components/data/when";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { type ApiErrorInfo, isApiError } from "@/lib/api/errors";
import { whoamiQuery } from "@/lib/queries";
import { newSpec, nextSystemNumber, type SystemSummary } from "@/lib/systems";
import { putSystem, systemHref, systemKeys, systemsQuery } from "@/lib/systems-queries";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { DefaultTag, NoDefaultBanner, SetDefaultButton, useSetDefault } from "./default-system";

const NARROW_HIDDEN = { size: "hidden md:table-cell", updated: "hidden lg:table-cell" };

function SystemsTable({ project, systems, admin }: { project: string; systems: SystemSummary[]; admin: boolean }) {
  const t = useTranslations("systems.list");
  const setDefault = useSetDefault(project);
  const pending = setDefault.isPending ? setDefault.variables : null;
  const { mutate } = setDefault;
  const action = useCallback(
    (system: SystemSummary) =>
      admin && !system.default ? (
        <SetDefaultButton name={system.name || system.slug} busy={pending === system.slug} onClick={() => mutate(system.slug)} />
      ) : null,
    [admin, pending, mutate],
  );
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
              <span className="flex min-w-0 items-center gap-2">
                <Link href={systemHref(project, system.slug)} className={`${NAME_LINK} truncate`} data-system-link={system.slug}>
                  {system.name || system.slug}
                </Link>
                {system.default ? <DefaultTag /> : null}
              </span>
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
      ...(admin
        ? [
            helper.display({
              id: "actions",
              header: () => <span className="sr-only">{t("columns.actions")}</span>,
              cell: (info) => action(info.row.original),
            }),
          ]
        : []),
    ]);
  }, [t, project, admin, action]);
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
        tags: system.default ? <DefaultTag /> : null,
        meta: <span className="font-mono">{system.slug}</span>,
        metaText: system.slug,
        actions: action(system),
        data: { "system-slug": system.slug },
      })}
    />
  );
}

/** How many times New system tries the next number when another system took its slug meanwhile (the hub's 409). */
const CREATE_TRIES = 5;

/**
 * New system, at once: "System {n}" at `system-{n}`, the lowest number no system of the project holds, created as
 * revision 1 with its entry alone, then its designer, where the name changes from the title. No form comes first.
 */
function useNewSystem(project: string, taken: readonly SystemSummary[] | null) {
  const t = useTranslations("systems.new");
  const failure = useWriteFailure();
  const queryClient = useQueryClient();
  const router = useRouter();
  return useMutation({
    mutationFn: async () => {
      const held = new Set((taken ?? []).map((system) => system.slug));
      for (let attempt = 1; ; attempt += 1) {
        const n = nextSystemNumber(held);
        const slug = `system-${n}`;
        try {
          return await putSystem(browserApi(), project, slug, newSpec(t("name", { n })), null);
        } catch (error) {
          if (!isApiError(error) || error.status !== 409 || attempt >= CREATE_TRIES) throw error;
          held.add(slug);
        }
      }
    },
    onSuccess: (written) => {
      void queryClient.invalidateQueries({ queryKey: systemKeys.list(project) });
      queryClient.setQueryData(systemKeys.one(project, written.slug), written);
      router.push(systemHref(project, written.slug));
    },
    onError: (error) => notifyFailure(t("failed"), failure(error)),
  });
}

/**
 * A project's agent systems (web/DESIGN.md, Agent systems): agents, graphs, a curator and an orchestrator joined on a
 * canvas, each system with its entry agent, its size and its revision. Every member reads them; an admin of the
 * project creates one here with one click and lands in its designer.
 */
export function SystemsPage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("systems");
  const state = useHubQuery(systemsQuery(browserApi, project), initialError);
  const me = useQuery(whoamiQuery(browserApi));
  const admin = me.data?.grants.some((grant) => grant.project === project && grant.role === "admin") ?? false;
  const systems = state.status === "success" ? state.data : null;
  const create = useNewSystem(project, systems);
  const newButton = admin ? (
    <Button onClick={() => create.mutate()} busy={create.isPending} disabled={!systems} data-testid="systems-new">
      <Plus aria-hidden="true" />
      {create.isPending ? t("new.creating") : t("newButton")}
    </Button>
  ) : null;
  return (
    <div className="flex flex-col gap-6" data-testid="systems-page">
      <PageHeader
        title={t("title")}
        tags={systems ? <Badge variant="secondary">{t("count", { count: systems.length })}</Badge> : null}
        actions={newButton}
      />
      <QueryView state={state} loading={<TableSkeleton rows={3} />}>
        {(rows) =>
          rows.length ? (
            <section aria-labelledby="systems-list-title" className="flex flex-col gap-4">
              {rows.some((system) => system.default) ? null : <NoDefaultBanner admin={admin} />}
              <h2 id="systems-list-title" className="sr-only">
                {t("list.caption", { project })}
              </h2>
              <DataCard testId="systems-card">
                <SystemsTable project={project} systems={rows} admin={admin} />
              </DataCard>
            </section>
          ) : (
            <EmptyState icon={Workflow} title={t("emptyTitle")} description={admin ? undefined : t("emptyReader")} testId="systems-empty">
              {newButton}
            </EmptyState>
          )
        }
      </QueryView>
    </div>
  );
}
