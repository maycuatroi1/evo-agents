"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronRight, SearchX, ShieldPlus, Users } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useCallback, useId, useMemo, useState } from "react";

import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { PageHeader } from "@/components/shell/page-header";
import { RoleBadge } from "@/components/shell/role-badge";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import type { Project } from "@/lib/api/client";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { projectsQuery } from "@/lib/queries";

import { HubAdminBadge, NotSignedInBadge } from "./badges";
import { type AdminUser, adminUsersQuery, filterMembers, memberHref, parseMemberFilters, tokensHref } from "./data";
import { GrantDialog, type GrantPreset } from "./grant-dialog";
import { NoticeArea, useNotice } from "./notice";
import { TableFrame } from "./table-frame";
import { useUrlView } from "./url-state";
import { When } from "./when";

// On a phone the login (a link to the member's page) and the grants are what matters; the rest is a tap away.
const NARROW_HIDDEN = {
  active_tokens: "hidden md:table-cell",
  last_seen: "hidden lg:table-cell",
  actions: "hidden sm:table-cell",
};

function Grants({ user }: { user: AdminUser }) {
  const t = useTranslations("admin.members");
  if (user.grants.length === 0) return <span className="text-muted-foreground">{t("noGrants")}</span>;
  return (
    <ul className="flex flex-col gap-1.5">
      {user.grants.map((grant) => (
        <li key={grant.project} className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="font-mono text-xs font-medium">{grant.project}</span>
          <RoleBadge role={grant.role} />
          <span className="font-mono text-xs text-muted-foreground">{grant.max_level}</span>
        </li>
      ))}
    </ul>
  );
}

function MembersTable({ users, onGrant }: { users: AdminUser[]; onGrant: (login: string) => void }) {
  const t = useTranslations("admin.members");
  const columns = useMemo(() => {
    const helper = dataTableColumns<AdminUser>();
    return helper.columns([
      helper.accessor("login", {
        header: () => t("columns.login"),
        sortFn: "alphanumeric",
        cell: (info) => {
          const user = info.row.original;
          return (
            <div className="flex flex-col items-start gap-1.5">
              <Link
                href={memberHref(user.login)}
                className="font-medium break-all text-primary underline-offset-4 hover:underline"
                data-testid={`member-link-${user.login}`}
              >
                {user.login}
              </Link>
              {user.admin || !user.signed_in ? (
                <span className="flex flex-wrap gap-1">
                  {user.admin ? <HubAdminBadge /> : null}
                  {!user.signed_in ? <NotSignedInBadge /> : null}
                </span>
              ) : null}
            </div>
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
        cell: (info) => (
          <Link
            href={tokensHref({ login: info.row.original.login })}
            className="tabular-nums text-primary underline-offset-4 hover:underline"
          >
            {info.getValue()}
          </Link>
        ),
      }),
      helper.accessor((row) => (row.last_seen_at ? new Date(row.last_seen_at) : new Date(0)), {
        id: "last_seen",
        header: () => t("columns.lastSeen"),
        sortFn: "datetime",
        cell: (info) => <When value={info.row.original.last_seen_at} never={t("never")} />,
      }),
      helper.display({
        id: "actions",
        header: () => <span className="sr-only">{t("columns.actions")}</span>,
        cell: (info) => {
          const login = info.row.original.login;
          return (
            <div className="flex items-center justify-end gap-1.5">
              <Button
                type="button"
                variant="outline"
                size="icon-lg"
                aria-label={t("grantTo", { login })}
                title={t("grantTo", { login })}
                onClick={() => onGrant(login)}
                data-testid={`grant-to-${login}`}
              >
                <ShieldPlus aria-hidden="true" />
              </Button>
              <Button asChild variant="ghost" size="lg">
                <Link href={memberHref(login)} aria-label={t("manage", { login })}>
                  {t("manageShort")}
                  <ChevronRight aria-hidden="true" />
                </Link>
              </Button>
            </div>
          );
        },
      }),
    ]);
  }, [t, onGrant]);
  return (
    <TableFrame>
      <DataTable
        data={users}
        columns={columns}
        caption={t("title")}
        getRowId={(row) => row.login}
        initialSorting={[{ id: "login", desc: false }]}
        columnClassNames={NARROW_HIDDEN}
        testId="members-table"
      />
    </TableFrame>
  );
}

/** Everyone on the hub with their grants; search and project filter in the URL; granting through a dialog. */
export function AdminMembers({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("admin.members");
  const ids = useId();
  const state = useHubQuery(adminUsersQuery(browserApi), initialError);
  const { data: projects } = useQuery(projectsQuery(browserApi));
  const { view, go } = useUrlView(parseMemberFilters);
  const { notice, show, clear } = useNotice();
  const [grantOpen, setGrantOpen] = useState(false);
  const [preset, setPreset] = useState<GrantPreset>({});
  const [query, setQuery] = useState(view.q);

  const openGrant = useCallback((login?: string) => {
    setPreset(login ? { login } : {});
    setGrantOpen(true);
  }, []);
  const users = state.status === "success" ? state.data : [];
  const shown = filterMembers(users, { q: query, project: view.project });
  const projectList: Project[] = projects ?? [];

  return (
    <>
      <PageHeader
        title={t("title")}
        description={t("description")}
        meta={
          <>
            {state.status === "success" ? <Badge variant="secondary">{t("count", { count: users.length })}</Badge> : null}
            {state.status === "success" && projects ? (
              <Button type="button" size="lg" onClick={() => openGrant()} data-testid="grant-open">
                <ShieldPlus aria-hidden="true" />
                {t("grant")}
              </Button>
            ) : null}
          </>
        }
      />
      <NoticeArea notice={notice} onDismiss={clear} />
      <QueryView state={state} loading={<TableSkeleton rows={6} />}>
        {(all) =>
          all.length === 0 ? (
            <EmptyState icon={Users} title={t("emptyTitle")} description={t("emptyDescription")} />
          ) : (
            <section aria-labelledby={`${ids}-list`} className="flex flex-col gap-4">
              <h2 id={`${ids}-list`} className="sr-only">
                {t("title")}
              </h2>
              <div
                role="search"
                aria-label={t("title")}
                className="grid grid-cols-1 gap-4 rounded-xl border bg-card p-4 sm:grid-cols-2"
                data-testid="member-filters"
              >
                <div className="flex min-w-0 flex-col gap-1.5">
                  <Label htmlFor={`${ids}-q`}>{t("search")}</Label>
                  <Input
                    id={`${ids}-q`}
                    type="search"
                    value={query}
                    placeholder={t("searchPlaceholder")}
                    autoComplete="off"
                    autoCapitalize="none"
                    spellCheck={false}
                    maxLength={100}
                    className="h-9"
                    onChange={(event) => {
                      setQuery(event.target.value);
                      go({ q: event.target.value.trim(), project: view.project }, "replace");
                    }}
                    aria-controls={`${ids}-results`}
                  />
                </div>
                <div className="flex min-w-0 flex-col gap-1.5">
                  <Label htmlFor={`${ids}-project`}>{t("project")}</Label>
                  <NativeSelect
                    id={`${ids}-project`}
                    value={view.project}
                    onChange={(event) => go({ q: query.trim(), project: event.target.value }, "replace")}
                    className="w-full [&_select]:h-9"
                    aria-controls={`${ids}-results`}
                  >
                    <NativeSelectOption value="">{t("allProjects")}</NativeSelectOption>
                    {projectList.map((project) => (
                      <NativeSelectOption key={project.name} value={project.name}>
                        {project.name}
                      </NativeSelectOption>
                    ))}
                  </NativeSelect>
                </div>
              </div>
              <p className="text-sm text-muted-foreground" aria-live="polite" aria-atomic="true" data-testid="members-shown">
                {t("shown", { shown: shown.length, total: all.length })}
              </p>
              <div id={`${ids}-results`}>
                {shown.length === 0 ? (
                  <EmptyState icon={SearchX} title={t("noMatchTitle")} description={t("noMatchDescription")} />
                ) : (
                  <MembersTable users={shown} onGrant={openGrant} />
                )}
              </div>
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
        onDone={show}
      />
    </>
  );
}
