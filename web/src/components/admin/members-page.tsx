"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronRight, ShieldPlus, Users } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useCallback, useId, useMemo, useState } from "react";

import { DataCard, DataToolbar } from "@/components/data/data-card";
import { ToolbarField, ToolbarFilters } from "@/components/data/filter-sheet";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";
import { SearchField } from "@/components/data/search-field";
import { VisibilityLevel } from "@/components/data/visibility";
import { notify } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { RoleBadge, roleLabelKey } from "@/components/shell/role-badge";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { type ActiveFilter, EmptyState, NoResults, TableSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import type { Project } from "@/lib/api/client";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { projectsQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { HubAdminBadge, NotSignedInBadge } from "./badges";
import { type AdminUser, adminUsersQuery, filterMembers, memberHref, parseMemberFilters, tokensHref } from "./data";
import { GrantDialog, type GrantPreset } from "./grant-dialog";
import { useUrlView } from "./url-state";
import { When } from "./when";

// On a phone the login (a link to the member's page) and the grants are what matters; the rest is a tap away.
const NARROW_HIDDEN = {
  active_tokens: "hidden md:table-cell",
  last_seen: "hidden lg:table-cell",
  actions: "hidden sm:table-cell",
};

/** A member's grants in two lines at most: the first one in full, then how many more and in which projects. */
function Grants({ user }: { user: AdminUser }) {
  const t = useTranslations("admin.members");
  const tRoles = useTranslations("roles");
  if (user.grants.length === 0) return <span className="text-fg-subtle">{t("noGrants")}</span>;
  const [first, ...rest] = user.grants;
  const all = user.grants.map((grant) => `${grant.project} (${tRoles(roleLabelKey(grant.role))})`).join(", ");
  return (
    <div className="flex min-w-0 flex-col gap-px">
      <span className="flex items-center gap-2 whitespace-nowrap">
        <span className="font-mono text-xs font-medium text-foreground">{first.project}</span>
        <RoleBadge role={first.role} />
        <VisibilityLevel level={first.max_level} className="text-xs text-muted-foreground" />
      </span>
      {rest.length > 0 ? (
        <span className="max-w-64 truncate text-xs leading-4 text-fg-subtle" title={all}>
          {t("moreGrants", { count: rest.length, projects: rest.map((grant) => grant.project).join(", ") })}
        </span>
      ) : null}
    </div>
  );
}

/** A member's grants in one line, for a phone's list: the first one with its role, then how many more. */
function useGrantLine() {
  const t = useTranslations("admin.members");
  const tRoles = useTranslations("roles");
  return (user: AdminUser): string => {
    if (user.grants.length === 0) return t("noGrants");
    const [first, ...rest] = user.grants;
    const grant = t("mobileGrant", { project: first.project, role: tRoles(roleLabelKey(first.role)) });
    return rest.length > 0 ? t("mobileMore", { grant, count: rest.length }) : grant;
  };
}

function MembersTable({ users, onGrant }: { users: AdminUser[]; onGrant: (login: string) => void }) {
  const t = useTranslations("admin.members");
  const grantLine = useGrantLine();
  const columns = useMemo(() => {
    const helper = dataTableColumns<AdminUser>();
    return helper.columns([
      helper.accessor("login", {
        header: () => t("columns.login"),
        sortFn: "alphanumeric",
        meta: { primary: true },
        cell: (info) => {
          const user = info.row.original;
          return (
            <CellMain>
              <Link
                href={memberHref(user.login)}
                className={cn(NAME_LINK, "truncate")}
                title={user.login}
                data-testid={`member-link-${user.login}`}
              >
                {user.login}
              </Link>
              {user.admin ? <HubAdminBadge /> : null}
              {!user.signed_in ? <NotSignedInBadge /> : null}
            </CellMain>
          );
        },
      }),
      helper.accessor((row) => row.grants.length, {
        id: "grants",
        header: () => t("columns.grants"),
        sortFn: "basic",
        cell: (info) => <Grants user={info.row.original} />,
      }),
      helper.accessor("active_tokens", {
        header: () => t("columns.tokens"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => (
          <Link
            href={tokensHref({ login: info.row.original.login })}
            className="text-brand underline-offset-4 hover:underline"
          >
            {info.getValue()}
          </Link>
        ),
      }),
      helper.accessor((row) => (row.last_seen_at ? new Date(row.last_seen_at) : new Date(0)), {
        id: "last_seen",
        header: () => t("columns.lastSeen"),
        sortFn: "datetime",
        meta: { numeric: true },
        cell: (info) => <When value={info.row.original.last_seen_at} never={t("never")} />,
      }),
      helper.display({
        id: "actions",
        header: () => <span className="sr-only">{t("columns.actions")}</span>,
        meta: { actions: true },
        cell: (info) => {
          const login = info.row.original.login;
          return (
            <>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label={t("grantTo", { login })}
                title={t("grantTo", { login })}
                onClick={() => onGrant(login)}
                data-testid={`grant-to-${login}`}
              >
                <ShieldPlus aria-hidden="true" />
              </Button>
              <Button asChild variant="ghost" size="sm">
                <Link href={memberHref(login)} aria-label={t("manage", { login })}>
                  {t("manageShort")}
                  <ChevronRight aria-hidden="true" />
                </Link>
              </Button>
            </>
          );
        },
      }),
    ]);
  }, [t, onGrant]);
  return (
    <DataTable
      data={users}
      columns={columns}
      caption={t("title")}
      getRowId={(row) => row.login}
      initialSorting={[{ id: "login", desc: false }]}
      columnClassNames={NARROW_HIDDEN}
      testId="members-table"
      // On a phone: the login opening the member's page (grants, tokens and Grant access are there), and their access.
      mobile={(user) => ({
        title: user.login,
        titleText: user.login,
        href: memberHref(user.login),
        tags: (
          <>
            {user.admin ? <HubAdminBadge /> : null}
            {!user.signed_in ? <NotSignedInBadge /> : null}
          </>
        ),
        meta: grantLine(user),
        metaText: grantLine(user),
        data: { login: user.login },
      })}
    />
  );
}

/** Everyone on the hub with their grants; search and project filter in the URL; granting through a dialog. */
export function AdminMembers({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("admin.members");
  const tStates = useTranslations("states.noResults");
  const ids = useId();
  const state = useHubQuery(adminUsersQuery(browserApi), initialError);
  const { data: projects } = useQuery(projectsQuery(browserApi));
  const { view, go } = useUrlView(parseMemberFilters);
  const [grantOpen, setGrantOpen] = useState(false);
  const [preset, setPreset] = useState<GrantPreset>({});

  const openGrant = useCallback((login?: string) => {
    setPreset(login ? { login } : {});
    setGrantOpen(true);
  }, []);
  const users = state.status === "success" ? state.data : [];
  const shown = filterMembers(users, view);
  const projectList: Project[] = projects ?? [];
  const memberFilters: ActiveFilter[] = [
    ...(view.q ? [{ label: tStates("search"), value: view.q }] : []),
    ...(view.project ? [{ label: t("project"), value: view.project }] : []),
  ];

  return (
    <>
      <PageHeader
        title={t("title")}
        tags={state.status === "success" ? <Badge variant="secondary">{t("count", { count: users.length })}</Badge> : null}
        actions={
          state.status === "success" && projects ? (
            <Button type="button" onClick={() => openGrant()} data-testid="grant-open">
              <ShieldPlus aria-hidden="true" />
              {t("grant")}
            </Button>
          ) : null
        }
      />
      <QueryView state={state} loading={<TableSkeleton rows={6} toolbar />}>
        {(all) =>
          all.length === 0 ? (
            <EmptyState icon={Users} title={t("emptyTitle")} description={t("emptyDescription")} />
          ) : (
            <section aria-labelledby={`${ids}-list`}>
              <h2 id={`${ids}-list`} className="sr-only">
                {t("title")}
              </h2>
              <DataCard
                toolbar={
                  <DataToolbar
                    label={t("title")}
                    count={t("shown", { shown: shown.length, total: all.length })}
                    countTestId="members-shown"
                  >
                    <SearchField
                      value={view.q}
                      onCommit={(q) => go({ q, project: view.project }, "replace")}
                      label={t("search")}
                      placeholder={t("searchPlaceholder")}
                      clearLabel={t("clearSearch")}
                      debounce={150}
                      maxLength={100}
                      controls={`${ids}-results`}
                      className="sm:w-64 max-md:flex-1"
                      testId="member-search"
                    />
                    <ToolbarFilters
                      active={view.project ? 1 : 0}
                      summary={t("shown", { shown: shown.length, total: all.length })}
                      onClear={() => go({ q: view.q }, "replace")}
                      testId="member-filters"
                    >
                      <ToolbarField label={t("project")}>
                        {(id) => (
                          <NativeSelect
                            id={id}
                            value={view.project}
                            onChange={(event) => go({ q: view.q, project: event.target.value }, "replace")}
                            className="w-full md:w-52"
                            aria-controls={`${ids}-results`}
                            data-testid="member-project"
                          >
                            <NativeSelectOption value="">{t("allProjects")}</NativeSelectOption>
                            {projectList.map((project) => (
                              <NativeSelectOption key={project.name} value={project.name}>
                                {project.name}
                              </NativeSelectOption>
                            ))}
                          </NativeSelect>
                        )}
                      </ToolbarField>
                    </ToolbarFilters>
                  </DataToolbar>
                }
              >
                <div id={`${ids}-results`}>
                  {shown.length === 0 ? (
                    <NoResults
                      title={t("noMatchTitle")}
                      filters={memberFilters}
                      onClear={() => go({}, "replace")}
                    />
                  ) : (
                    <MembersTable users={shown} onGrant={openGrant} />
                  )}
                </div>
              </DataCard>
            </section>
          )
        }
      </QueryView>
      <GrantDialog
        open={grantOpen}
        onOpenChange={setGrantOpen}
        users={users}
        projects={projectList}
        preset={preset}
        onDone={notify}
      />
    </>
  );
}
