"use client";

import { Database, FolderGit2, type LucideIcon, Shapes, Users } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { PageSkeleton } from "@/components/states/states";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { adminStatsQuery } from "@/lib/queries";

type Group = "access" | "projects" | "content" | "other";
type KnownTable =
  | "users"
  | "tokens"
  | "grants"
  | "audit"
  | "projects"
  | "project_repos"
  | "project_sinks"
  | "memories"
  | "memory_revisions"
  | "plans"
  | "plan_revisions"
  | "skills"
  | "skill_versions"
  | "kg_ingests";

const GROUPS: { id: Group; icon: LucideIcon; tables: readonly KnownTable[] }[] = [
  { id: "access", icon: Users, tables: ["users", "tokens", "grants", "audit"] },
  { id: "projects", icon: FolderGit2, tables: ["projects", "project_repos", "project_sinks"] },
  {
    id: "content",
    icon: Shapes,
    tables: ["memories", "memory_revisions", "plans", "plan_revisions", "skills", "skill_versions", "kg_ingests"],
  },
];

const KNOWN = new Set<string>(GROUPS.flatMap((group) => group.tables));

function isKnown(table: string): table is KnownTable {
  return KNOWN.has(table);
}

function Stats({ stats }: { stats: Record<string, number> }) {
  const t = useTranslations("admin");
  const format = useFormatter();
  const others = Object.keys(stats)
    .filter((table) => !isKnown(table))
    .sort();
  const groups = [
    ...GROUPS.map((group) => ({ ...group, tables: group.tables.filter((table) => table in stats) as string[] })),
    { id: "other" as const, icon: Database, tables: others },
  ].filter((group) => group.tables.length > 0);

  return (
    <div className="grid gap-6 xl:grid-cols-3" data-testid="admin-stats">
      {groups.map((group) => (
        <Card key={group.id}>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <group.icon className="size-4 text-muted-foreground" aria-hidden="true" />
              <h2>{t(`groups.${group.id}`)}</h2>
            </CardTitle>
          </CardHeader>
          <CardContent>
            <dl className="grid grid-cols-2 gap-3">
              {group.tables.map((table) => (
                <div key={table} className="flex flex-col gap-1 rounded-lg border bg-background/60 px-3 py-2.5">
                  <dt className="truncate text-xs text-muted-foreground">
                    {isKnown(table) ? t(`tables.${table}`) : <span className="font-mono">{table}</span>}
                  </dt>
                  <dd className="font-mono text-xl font-semibold tabular-nums" aria-label={t("rows", { count: stats[table] })}>
                    {format.number(stats[table])}
                  </dd>
                </div>
              ))}
            </dl>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

export function AdminOverview({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("admin");
  const state = useHubQuery(adminStatsQuery(browserApi), initialError);
  return (
    <>
      <PageHeader title={t("title")} description={t("description")} />
      <QueryView state={state} loading={<PageSkeleton />}>
        {(stats) => <Stats stats={stats} />}
      </QueryView>
    </>
  );
}
