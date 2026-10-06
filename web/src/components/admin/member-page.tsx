"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowRight, FolderLock, KeyRound, Pencil, ScrollText, ShieldMinus, ShieldPlus } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useCallback, useMemo, useState } from "react";

import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { projectHref } from "@/components/shell/nav";
import { PageHeader } from "@/components/shell/page-header";
import { RoleBadge } from "@/components/shell/role-badge";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, NotFoundState, PageSkeleton, TableSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";
import type { Project } from "@/lib/api/client";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { projectsQuery } from "@/lib/queries";

import { AuditTable } from "./audit-table";
import { HubAdminBadge, NotSignedInBadge } from "./badges";
import { ConfirmAction } from "./confirm-action";
import {
  type AdminUser,
  adminUsersQuery,
  auditHref,
  auditQuery,
  deleteGrant,
  memberAuditParams,
  memberHref,
  memberTokensParams,
  type Role,
  ROLES,
  tokensHref,
  tokensQuery,
  type UserGrant,
} from "./data";
import { GrantDialog, type GrantPreset } from "./grant-dialog";
import { type Notice, NoticeArea, useNotice, useWriteFailure } from "./notice";
import { TokensTable } from "./tokens-table";
import { useAdminWrite } from "./use-admin-write";
import { When } from "./when";

const GRANT_HIDDEN = { max_level: "hidden sm:table-cell", granted_by: "hidden md:table-cell", granted_at: "hidden lg:table-cell" };

function isRole(value: string): value is Role {
  return (ROLES as readonly string[]).includes(value);
}

function Section({
  id,
  icon: Icon,
  title,
  description,
  action,
  children,
}: {
  id: string;
  icon: typeof KeyRound;
  title: string;
  description: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Card aria-labelledby={`${id}-title`} role="region" data-testid={id}>
      <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex flex-col gap-1">
          <CardTitle className="flex items-center gap-2">
            <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
            <h2 id={`${id}-title`}>{title}</h2>
          </CardTitle>
          <CardDescription>{description}</CardDescription>
        </div>
        {action ? <div className="shrink-0">{action}</div> : null}
      </CardHeader>
      <CardContent className="flex flex-col gap-4">{children}</CardContent>
    </Card>
  );
}

function GrantsTable({
  user,
  onChange,
  onRevoke,
}: {
  user: AdminUser;
  onChange: (grant: UserGrant) => void;
  onRevoke: (grant: UserGrant) => void;
}) {
  const t = useTranslations("admin.member");
  const columns = useMemo(() => {
    const helper = dataTableColumns<UserGrant>();
    return helper.columns([
      helper.accessor("project", {
        header: () => t("columns.project"),
        sortFn: "alphanumeric",
        cell: (info) => (
          <Link href={projectHref(info.getValue())} className="font-mono text-sm font-medium text-brand underline-offset-4 hover:underline">
            {info.getValue()}
          </Link>
        ),
      }),
      helper.accessor("role", {
        header: () => t("columns.role"),
        sortFn: "text",
        cell: (info) => (
          <span className="flex flex-col items-start gap-1">
            <RoleBadge role={info.getValue()} />
            {/* The level column is hidden on a phone: show the level here instead. */}
            <span className="font-mono text-xs text-muted-foreground sm:hidden">{info.row.original.max_level}</span>
          </span>
        ),
      }),
      helper.accessor("max_level", {
        header: () => t("columns.maxLevel"),
        enableSorting: false,
        cell: (info) => <span className="font-mono text-xs">{info.getValue()}</span>,
      }),
      helper.accessor("granted_by", {
        header: () => t("columns.grantedBy"),
        enableSorting: false,
        cell: (info) => {
          const by = info.getValue();
          return by ? (
            <Link href={memberHref(by)} className="text-brand underline-offset-4 hover:underline">
              {by}
            </Link>
          ) : (
            <span className="text-muted-foreground">{t("unknown")}</span>
          );
        },
      }),
      helper.accessor("granted_at", {
        header: () => t("columns.grantedAt"),
        enableSorting: false,
        cell: (info) => <When value={info.getValue()} />,
      }),
      helper.display({
        id: "actions",
        header: () => <span className="sr-only">{t("columns.actions")}</span>,
        cell: (info) => {
          const grant = info.row.original;
          const names = { login: user.login, project: grant.project };
          return (
            <div className="flex flex-wrap items-center justify-end gap-1.5">
              <Button
                type="button"
                variant="outline"
                size="lg"
                aria-label={t("changeLabel", names)}
                onClick={() => onChange(grant)}
                data-testid={`change-grant-${grant.project}`}
              >
                <Pencil aria-hidden="true" />
                <span className="hidden sm:inline">{t("change")}</span>
              </Button>
              <Button
                type="button"
                variant="outline"
                size="lg"
                className="text-danger hover:text-danger"
                aria-label={t("revokeLabel", names)}
                onClick={() => onRevoke(grant)}
                data-testid={`revoke-grant-${grant.project}`}
              >
                <ShieldMinus aria-hidden="true" />
                <span className="hidden sm:inline">{t("revoke")}</span>
              </Button>
            </div>
          );
        },
      }),
    ]);
  }, [t, user.login, onChange, onRevoke]);
  return (
    <DataTable
      data={user.grants}
      columns={columns}
      caption={t("grantsTitle")}
      getRowId={(row) => row.project}
      initialSorting={[{ id: "project", desc: false }]}
      columnClassNames={GRANT_HIDDEN}
      testId="member-grants"
    />
  );
}

function RevokeGrant({
  login,
  grant,
  open,
  onOpenChange,
  onNotice,
}: {
  login: string;
  grant: UserGrant | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onNotice: (notice: Notice) => void;
}) {
  const t = useTranslations("admin.revokeGrant");
  const failure = useWriteFailure();
  const write = useAdminWrite((api, project: string) => deleteGrant(api, project, login));
  const names = { login, project: grant?.project ?? "" };
  const confirm = async () => {
    if (!grant) return;
    try {
      await write.mutateAsync(grant.project);
    } catch (error) {
      const failed = failure(error, { 404: t("notFound") });
      if (failed.status === 404) {
        onOpenChange(false);
        onNotice({ tone: "error", text: failed.text });
      }
      return;
    }
    onOpenChange(false);
    onNotice({ tone: "success", text: t("success", names) });
  };
  return (
    <ConfirmAction
      open={open}
      onOpenChange={onOpenChange}
      title={t("title", names)}
      description={<p>{t("description", names)}</p>}
      confirmLabel={t("confirm")}
      pendingLabel={t("pending")}
      cancelLabel={t("cancel")}
      pending={write.isPending}
      error={write.isError && open ? failure(write.error, { 404: t("notFound") }) : null}
      onConfirm={() => void confirm()}
      testId="revoke-grant-dialog"
    />
  );
}

function MemberView({ user, projects }: { user: AdminUser; projects: Project[] }) {
  const t = useTranslations("admin.member");
  const format = useFormatter();
  const { notice, show: onNotice, clear } = useNotice();
  const tMembers = useTranslations("admin.members");
  const tTokens = useTranslations("admin.tokens");
  const tAudit = useTranslations("admin.audit");
  const tokens = useHubQuery(tokensQuery(browserApi, memberTokensParams(user.login)));
  const activity = useHubQuery(auditQuery(browserApi, memberAuditParams(user.login)));
  const [grantOpen, setGrantOpen] = useState(false);
  const [preset, setPreset] = useState<GrantPreset>({ login: user.login, lockLogin: true });
  const [revoking, setRevoking] = useState<UserGrant | null>(null);
  const [revokeOpen, setRevokeOpen] = useState(false);
  const [revokeRun, setRevokeRun] = useState(0); // a fresh confirmation, without an earlier failure, each time

  const change = useCallback(
    (grant: UserGrant) => {
      setPreset({
        login: user.login,
        lockLogin: true,
        project: grant.project,
        role: isRole(grant.role) ? grant.role : undefined,
        maxLevel: grant.max_level,
      });
      setGrantOpen(true);
    },
    [user.login],
  );
  const revoke = useCallback((grant: UserGrant) => {
    setRevoking(grant);
    setRevokeRun((run) => run + 1);
    setRevokeOpen(true);
  }, []);
  const grantNew = () => {
    setPreset({ login: user.login, lockLogin: true });
    setGrantOpen(true);
  };

  return (
    <>
      <PageHeader
        eyebrow={t("eyebrow")}
        title={<span className="font-mono">{user.login}</span>}
        description={t("summary", {
          created: format.dateTime(new Date(user.created_at), { dateStyle: "medium" }),
          lastSeen: user.last_seen_at
            ? format.dateTime(new Date(user.last_seen_at), { dateStyle: "medium", timeStyle: "short" })
            : tMembers("never"),
        })}
        meta={
          <>
            {user.admin ? <HubAdminBadge /> : null}
            {!user.signed_in ? <NotSignedInBadge /> : null}
            <Button type="button" size="lg" onClick={grantNew} data-testid="grant-open">
              <ShieldPlus aria-hidden="true" />
              {tMembers("grant")}
            </Button>
          </>
        }
      />
      <NoticeArea notice={notice} onDismiss={clear} />
      <Section
        id="member-grants-section"
        icon={FolderLock}
        title={t("grantsTitle")}
        description={t("grantsDescription")}
      >
        {user.grants.length === 0 ? (
          <EmptyState
            icon={FolderLock}
            title={t("grantsEmptyTitle")}
            description={t("grantsEmptyDescription", { login: user.login })}
            className="border-solid"
          />
        ) : (
          <GrantsTable user={user} onChange={change} onRevoke={revoke} />
        )}
      </Section>
      <Section
        id="member-tokens-section"
        icon={KeyRound}
        title={t("tokensTitle")}
        description={t("tokensDescription")}
        action={
          <Button asChild variant="ghost" size="lg">
            <Link href={tokensHref({ login: user.login, state: "any" })}>
              {t("allTokens", { login: user.login })}
              <ArrowRight aria-hidden="true" />
            </Link>
          </Button>
        }
      >
        <QueryView state={tokens} loading={<TableSkeleton rows={2} />}>
          {(page) =>
            page.items.length === 0 ? (
              <p className="text-sm text-muted-foreground">{tTokens("emptyTitle")}</p>
            ) : (
              <TokensTable tokens={page.items} caption={t("tokensTitle")} showLogin={false} onNotice={onNotice} testId="member-tokens" />
            )
          }
        </QueryView>
      </Section>
      <Section
        id="member-activity-section"
        icon={ScrollText}
        title={t("activityTitle")}
        description={t("activityDescription")}
        action={
          <Button asChild variant="ghost" size="lg">
            <Link href={auditHref({ actor: user.login })}>
              {t("allActivity")}
              <ArrowRight aria-hidden="true" />
            </Link>
          </Button>
        }
      >
        <QueryView state={activity} loading={<TableSkeleton rows={3} />}>
          {(page) =>
            page.items.length === 0 ? (
              <p className="text-sm text-muted-foreground">{tAudit("emptyTitle")}</p>
            ) : (
              <AuditTable rows={page.items} caption={t("activityTitle")} testId="member-activity" />
            )
          }
        </QueryView>
      </Section>
      <GrantDialog open={grantOpen} onOpenChange={setGrantOpen} users={[user]} projects={projects} preset={preset} onDone={onNotice} />
      <RevokeGrant
        key={revokeRun}
        login={user.login}
        grant={revoking}
        open={revokeOpen}
        onOpenChange={setRevokeOpen}
        onNotice={onNotice}
      />
    </>
  );
}

/** One member: their grants (change, revoke), their live tokens (revoke) and what they did last. */
export function AdminMember({ login, initialError }: { login: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("admin.member");
  const state = useHubQuery(adminUsersQuery(browserApi), initialError);
  const { data: projects } = useQuery(projectsQuery(browserApi));
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(users) => {
        const user = users.find((u) => u.login.toLowerCase() === login.toLowerCase());
        if (!user)
          return (
            <NotFoundState title={t("notFoundTitle", { login })} description={t("notFoundDescription")} />
          );
        return <MemberView user={user} projects={projects ?? []} />;
      }}
    </QueryView>
  );
}
