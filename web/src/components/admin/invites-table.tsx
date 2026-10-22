"use client";

import { Ban } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useCallback, useMemo, useState } from "react";

import type { DataListRow } from "@/components/data/data-list";
import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";
import { VisibilityLevel } from "@/components/data/visibility";
import { ConfirmAction } from "@/components/feedback/confirm-action";
import type { Notice } from "@/components/feedback/toast";
import { RoleBadge } from "@/components/shell/role-badge";
import { Button } from "@/components/ui/button";

import { InviteKindBadge, InviteStatusBadge } from "./badges";
import { memberHref } from "./data";
import { type InviteRow, revocable, revokeInvite } from "./invite-data";
import { useWriteFailure } from "./notice";
import { useAdminWrite } from "./use-admin-write";
import { When } from "./when";

const NARROW_HIDDEN = {
  id: "hidden sm:table-cell",
  createdBy: "hidden xl:table-cell",
  expires: "hidden md:table-cell",
  acceptedBy: "hidden lg:table-cell",
};

type Props = {
  invites: InviteRow[];
  caption: string;
  onNotice: (notice: Notice) => void;
};

/** What a link gives: a role on a project up to a level, or one project of the accepter's own (and whether it was). */
function Gives({ invite }: { invite: InviteRow }) {
  const t = useTranslations("admin.invites");
  if (invite.kind === "join") {
    return (
      <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <span className="max-w-48 truncate font-mono text-[13px]" title={invite.project ?? undefined}>
          {invite.project}
        </span>
        <RoleBadge role={invite.role} />
        {invite.max_level ? (
          <span className="text-xs text-muted-foreground">
            <VisibilityLevel level={invite.max_level} />
          </span>
        ) : null}
      </span>
    );
  }
  // Once accepted, the right is used (it names the project) or waits for a registration, unless it was revoked.
  const right = invite.project ? t("rightUsed", { project: invite.project }) : invite.status === "accepted" ? t("rightUnused") : null;
  if (!right) return <span>{t("registerGives")}</span>;
  return (
    <span className="flex flex-col">
      <span>{t("registerGives")}</span>
      <span className="text-xs text-muted-foreground [overflow-wrap:anywhere]">{right}</span>
    </span>
  );
}

function Login({ login, empty }: { login: string | null; empty: string }) {
  if (!login) return <span className="text-fg-subtle">{empty}</span>;
  return (
    <Link href={memberHref(login)} className={NAME_LINK}>
      {login}
    </Link>
  );
}

/** Invite links, newest first, with their revoke action while the API would take it. */
export function InvitesTable({ invites, caption, onNotice }: Props) {
  const t = useTranslations("admin.invites");
  const tRevoke = useTranslations("admin.invites.revokeDialog");
  const format = useFormatter();
  const failure = useWriteFailure();
  const write = useAdminWrite((api, id: number) => revokeInvite(api, id));
  const [target, setTarget] = useState<InviteRow | null>(null);
  const [open, setOpen] = useState(false);
  const { reset } = write;
  const startRevoke = useCallback(
    (invite: InviteRow) => {
      reset();
      setTarget(invite);
      setOpen(true);
    },
    [reset],
  );

  const revokeButton = useCallback(
    (invite: InviteRow, icon: boolean) =>
      revocable(invite) ? (
        <Button
          type="button"
          variant="quiet-danger"
          size={icon ? "icon" : "sm"}
          aria-label={t("revokeLabel", { id: invite.id })}
          title={icon ? t("revokeLabel", { id: invite.id }) : undefined}
          onClick={() => startRevoke(invite)}
          data-testid={`revoke-invite-${invite.id}`}
        >
          <Ban aria-hidden="true" />
          {icon ? null : t("revoke")}
        </Button>
      ) : null,
    [t, startRevoke],
  );

  const columns = useMemo(() => {
    const helper = dataTableColumns<InviteRow>();
    return helper.columns([
      helper.accessor("id", {
        header: () => t("columns.id"),
        enableSorting: false,
        cell: (info) => (
          <span className="font-mono text-[13px] tabular-nums" data-testid={`invite-${info.getValue()}`}>
            #{info.getValue()}
          </span>
        ),
      }),
      helper.accessor("kind", {
        header: () => t("columns.kind"),
        enableSorting: false,
        cell: (info) => <InviteKindBadge kind={info.getValue()} />,
      }),
      helper.display({
        id: "gives",
        header: () => t("columns.gives"),
        cell: (info) => <Gives invite={info.row.original} />,
      }),
      helper.accessor("for_login", {
        id: "forLogin",
        header: () => t("columns.forLogin"),
        enableSorting: false,
        cell: (info) => <Login login={info.getValue()} empty={t("anyone")} />,
      }),
      helper.accessor("created_by", {
        id: "createdBy",
        header: () => t("columns.createdBy"),
        enableSorting: false,
        cell: (info) => <Login login={info.getValue()} empty={t("nobody")} />,
      }),
      helper.accessor("expires_at", {
        id: "expires",
        header: () => t("columns.expires"),
        enableSorting: false,
        meta: { numeric: true },
        cell: (info) => <When value={info.getValue()} short />,
      }),
      helper.accessor("status", {
        header: () => t("columns.status"),
        enableSorting: false,
        cell: (info) => <InviteStatusBadge status={info.getValue()} />,
      }),
      helper.accessor("accepted_by", {
        id: "acceptedBy",
        header: () => t("columns.acceptedBy"),
        enableSorting: false,
        cell: (info) => <Login login={info.getValue()} empty={t("nobody")} />,
      }),
      helper.display({
        id: "actions",
        header: () => <span className="sr-only">{t("columns.actions")}</span>,
        meta: { actions: true },
        cell: (info) => revokeButton(info.row.original, false),
      }),
    ]);
  }, [t, revokeButton]);

  const confirm = async () => {
    if (!target) return;
    try {
      await write.mutateAsync(target.id); // resolves once the list shows the hub's new state
    } catch (error) {
      const failed = failure(error, { 404: tRevoke("notFound"), 409: tRevoke("conflict") });
      if (failed.status === 404 || failed.status === 409) {
        setOpen(false); // nothing left to do here: say so in a toast, over the reloaded list
        onNotice({ tone: "error", text: tRevoke("failed", { id: target.id }), description: failed.text, requestId: failed.requestId });
      }
      return; // anything else is shown in the dialog, which stays open
    }
    setOpen(false);
    onNotice({ tone: "success", text: tRevoke("successTitle", { id: target.id }), description: tRevoke("success") });
  };

  const error = write.isError && open ? failure(write.error, { 404: tRevoke("notFound"), 409: tRevoke("conflict") }) : null;
  // On a phone: the link's kind and number, its status, what it gives and to whom, and Revoke as an icon.
  const mobile = (invite: InviteRow): DataListRow => {
    const gives = invite.kind === "join" ? (invite.project ?? "") : t("registerGives");
    const meta = format.list([gives, invite.for_login ?? t("anyone")], { type: "unit" });
    return {
      title: t("mobileTitle", { kind: t(`kinds.${invite.kind}`), id: invite.id }),
      status: <InviteStatusBadge status={invite.status} />,
      meta,
      metaText: meta,
      actions: revokeButton(invite, true),
      data: { "invite-id": invite.id },
    };
  };

  return (
    <>
      <DataTable
        data={invites}
        columns={columns}
        caption={caption}
        getRowId={(row) => String(row.id)}
        columnClassNames={NARROW_HIDDEN}
        testId="invites-table"
        mobile={mobile}
      />
      <ConfirmAction
        open={open}
        onOpenChange={setOpen}
        title={target ? tRevoke("title", { id: target.id }) : ""}
        description={
          target ? (
            <>
              <p>
                {target.status === "accepted" && target.accepted_by
                  ? tRevoke("right", { login: target.accepted_by })
                  : tRevoke("open")}
              </p>
              <p>{tRevoke("audit")}</p>
            </>
          ) : null
        }
        confirmLabel={tRevoke("confirm")}
        pendingLabel={tRevoke("pending")}
        cancelLabel={tRevoke("cancel")}
        pending={write.isPending}
        error={error}
        onConfirm={() => void confirm()}
        testId="revoke-invite-dialog"
      />
    </>
  );
}
