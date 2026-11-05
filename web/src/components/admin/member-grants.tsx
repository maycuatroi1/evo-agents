"use client";

import { Pencil, ShieldMinus } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useMemo } from "react";

import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";
import { VisibilityLevel } from "@/components/data/visibility";
import { projectHref } from "@/components/shell/nav";
import { RoleBadge } from "@/components/shell/role-badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { type AdminUser, memberHref, type UserGrant } from "./data";
import { When } from "./when";

const GRANT_HIDDEN = { max_level: "hidden sm:table-cell", granted_by: "hidden md:table-cell", granted_at: "hidden lg:table-cell" };

export function GrantsTable({
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
          <Link href={projectHref(info.getValue())} className={cn(NAME_LINK, "font-mono text-sm")}>
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
            {/* The visibility column is hidden on a phone: show the visibility here instead. */}
            <VisibilityLevel level={info.row.original.max_level} className="text-xs text-muted-foreground sm:hidden" />
          </span>
        ),
      }),
      helper.accessor("max_level", {
        header: () => t("columns.visibility"),
        enableSorting: false,
        cell: (info) => <VisibilityLevel level={info.getValue()} />,
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
