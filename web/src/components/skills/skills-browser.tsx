"use client";

import { Blocks, Globe, Puzzle } from "lucide-react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo } from "react";

import { DataCard, DataToolbar } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";
import { SearchField } from "@/components/data/search-field";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, NoResults, TableSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { projectQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { type Skill, skillHref, type SkillPlace, skillsQuery } from "./queries";
import { ByteSize, SourceLink } from "./skill-bits";
import type { RepoOrigin } from "./source";

const NARROW_HIDDEN = { size: "hidden md:table-cell", source: "hidden lg:table-cell", published: "hidden sm:table-cell" };

/** Whether a skill matches the filter text: its name or description holds every word, ignoring case. */
export function matchesSkill(skill: Pick<Skill, "name" | "description">, text: string): boolean {
  const haystack = `${skill.name} ${skill.description}`.toLocaleLowerCase();
  return text
    .toLocaleLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((word) => haystack.includes(word));
}

function SkillsTable({ skills, place, repos }: { skills: Skill[]; place: SkillPlace; repos: readonly RepoOrigin[] }) {
  const t = useTranslations("skills");
  const format = useFormatter();
  const columns = useMemo(() => {
    const helper = dataTableColumns<Skill>();
    return helper.columns([
      helper.accessor("name", {
        header: () => t("columns.name"),
        sortFn: "alphanumeric",
        meta: { primary: true },
        cell: (info) => (
          <CellMain sub={info.row.original.description} subTitle={info.row.original.description}>
            <Link
              href={skillHref(place, info.getValue())}
              className={cn(NAME_LINK, "truncate font-mono text-[13px]")}
              title={info.getValue()}
              data-skill-name={info.getValue()}
            >
              {info.getValue()}
            </Link>
          </CellMain>
        ),
      }),
      helper.accessor("version", {
        header: () => t("columns.version"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => <span className="font-mono text-xs">v{info.getValue()}</span>,
      }),
      helper.accessor("size", {
        header: () => t("columns.size"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => <ByteSize bytes={info.getValue()} />,
      }),
      helper.accessor((row) => row.source_repo ?? "", {
        id: "source",
        header: () => t("columns.source"),
        enableSorting: false,
        cell: (info) => (
          <SourceLink repo={info.row.original.source_repo} commit={info.row.original.source_commit} repos={repos} />
        ),
      }),
      helper.accessor((row) => new Date(row.published_at), {
        id: "published",
        header: () => t("columns.published"),
        sortFn: "datetime",
        meta: { numeric: true },
        cell: (info) => (
          <div className="flex flex-col items-end gap-px">
            <time dateTime={info.row.original.published_at} className="whitespace-nowrap">
              {format.dateTime(info.getValue(), { dateStyle: "medium" })}
            </time>
            <span className="text-xs leading-4 whitespace-nowrap text-fg-subtle">
              {t("by", { login: info.row.original.published_by })}
            </span>
          </div>
        ),
      }),
    ]);
  }, [t, format, place, repos]);

  return (
    <DataTable
      data={skills}
      columns={columns}
      caption={place.kind === "project" ? t("projectTitle") : t("globalTitle")}
      getRowId={(row) => row.name}
      initialSorting={[{ id: "name", desc: false }]}
      columnClassNames={NARROW_HIDDEN}
      testId="skills-table"
    />
  );
}

/** The skills of a project, or the hub's global ones, with their latest version. */
export function SkillsBrowser({ place, initialError }: { place: SkillPlace; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("skills");
  const tStates = useTranslations("states.noResults");
  const params = useSearchParams();
  const pathname = usePathname();
  const filter = (params.get("q") ?? "").trim().slice(0, 200);
  const state = useHubQuery(skillsQuery(browserApi, place), initialError);
  // A project's repos tell which GitHub repo a source names; failing to read them only costs the links.
  const project = useHubQuery({
    ...projectQuery(browserApi, place.kind === "project" ? place.project : ""),
    enabled: place.kind === "project",
  });
  const repos = project.status === "success" ? project.data.repos : [];
  const count = state.status === "success" ? state.data.length : null;
  // The History API: Next.js syncs useSearchParams with it, and the list is filtered here without a server round trip.
  const setFilter = (q: string) =>
    window.history.replaceState(null, "", `${pathname}${q ? `?${new URLSearchParams({ q })}` : ""}`);

  return (
    <>
      <PageHeader
        title={place.kind === "project" ? t("projectTitle") : t("globalTitle")}
        tags={count !== null ? <Badge variant="secondary">{t("count", { count })}</Badge> : null}
      />
      <QueryView state={state} loading={<TableSkeleton rows={5} toolbar />}>
        {(skills) => {
          if (skills.length === 0) {
            return place.kind === "project" ? (
              <EmptyState
                icon={Puzzle}
                title={t("empty.projectTitle")}
                description={t.rich("empty.projectDescription", {
                  code: (chunks) => <code className="font-mono">{chunks}</code>,
                  project: place.project,
                })}
              >
                <Button asChild variant="outline">
                  <Link href="/skills">
                    <Globe aria-hidden="true" />
                    {t("empty.toGlobal")}
                  </Link>
                </Button>
              </EmptyState>
            ) : (
              <EmptyState
                icon={Blocks}
                title={t("empty.globalTitle")}
                description={t.rich("empty.globalDescription", {
                  code: (chunks) => <code className="font-mono">{chunks}</code>,
                })}
              />
            );
          }
          const shown = filter ? skills.filter((skill) => matchesSkill(skill, filter)) : skills;
          const caption = place.kind === "project" ? t("projectTitle") : t("globalTitle");
          return (
            <DataCard
              toolbar={
                <DataToolbar
                  label={caption}
                  count={
                    filter
                      ? t("summary.filtered", { shown: shown.length, count: skills.length })
                      : t("summary.all", { count: skills.length })
                  }
                  countTestId="skills-summary"
                >
                  <SearchField
                    value={filter}
                    onCommit={setFilter}
                    label={t("filter.label")}
                    placeholder={t("filter.placeholder")}
                    clearLabel={t("filter.clear")}
                    debounce={150}
                    maxLength={200}
                    className="sm:w-64"
                    testId="skills-filter"
                  />
                </DataToolbar>
              }
            >
              {shown.length === 0 ? (
                <NoResults
                  title={t("noResults.title", { q: filter })}
                  filters={[{ label: tStates("search"), value: filter }]}
                  onClear={() => setFilter("")}
                />
              ) : (
                <SkillsTable skills={shown} place={place} repos={repos} />
              )}
            </DataCard>
          );
        }}
      </QueryView>
    </>
  );
}
