"use client";

import { useQuery } from "@tanstack/react-query";
import { FolderGit2 } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo } from "react";

import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";
import { VisibilityLevel } from "@/components/data/visibility";
import { projectHref } from "@/components/shell/nav";
import { PageHeader } from "@/components/shell/page-header";
import { RoleBadge } from "@/components/shell/role-badge";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { browserApi } from "@/lib/api/browser";
import type { Project } from "@/lib/api/client";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { projectsQuery, whoamiQuery } from "@/lib/queries";

const NARROW_HIDDEN = { repos: "hidden md:table-cell", updated: "hidden lg:table-cell", max_level: "hidden sm:table-cell" };

function ProjectsTable({ projects }: { projects: Project[] }) {
  const t = useTranslations("home");
  const format = useFormatter();
  const columns = useMemo(() => {
    const helper = dataTableColumns<Project>();
    return helper.columns([
      helper.accessor("name", {
        header: () => t("columns.name"),
        sortFn: "alphanumeric",
        cell: (info) => (
          <Link
            href={projectHref(info.getValue())}
            className={NAME_LINK}
            aria-label={t("open", { name: info.getValue() })}
          >
            {info.getValue()}
          </Link>
        ),
      }),
      helper.accessor((row) => row.role ?? "", {
        id: "role",
        header: () => t("columns.role"),
        sortFn: "text",
        cell: (info) => <RoleBadge role={info.row.original.role} />,
      }),
      helper.accessor((row) => row.max_level ?? "", {
        id: "max_level",
        header: () => t("columns.visibility"),
        enableSorting: false,
        cell: (info) => <VisibilityLevel level={info.getValue()} />,
      }),
      helper.accessor((row) => row.repos.length, {
        id: "repos",
        header: () => t("columns.repos"),
        sortFn: "basic",
        cell: (info) => <span className="tabular-nums">{info.getValue()}</span>,
      }),
      helper.accessor((row) => new Date(row.updated_at), {
        id: "updated",
        header: () => t("columns.updated"),
        sortFn: "datetime",
        cell: (info) => (
          <time dateTime={info.row.original.updated_at} className="text-muted-foreground tabular-nums">
            {format.dateTime(info.getValue(), { dateStyle: "medium", timeStyle: "short" })}
          </time>
        ),
      }),
    ]);
  }, [t, format]);

  return (
    <DataTable
      data={projects}
      columns={columns}
      caption={t("title")}
      getRowId={(row) => row.name}
      initialSorting={[{ id: "name", desc: false }]}
      columnClassNames={NARROW_HIDDEN}
      testId="projects-table"
    />
  );
}

export function ProjectsHome({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("home");
  const state = useHubQuery(projectsQuery(browserApi), initialError);
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const count = state.status === "success" ? state.data.length : null;

  return (
    <>
      <PageHeader title={t("title")} tags={count !== null ? <Badge variant="secondary">{t("count", { count })}</Badge> : null} />
      <QueryView state={state} loading={<TableSkeleton rows={4} />}>
        {(projects) =>
          projects.length === 0 ? (
            <EmptyState
              icon={FolderGit2}
              title={t("emptyTitle")}
              description={t("emptyDescription", { login: me?.login ?? "" })}
            />
          ) : (
            <ProjectsTable projects={projects} />
          )
        }
      </QueryView>
    </>
  );
}
