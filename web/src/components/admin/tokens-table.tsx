"use client";

import { KeyRound } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useCallback, useMemo, useState } from "react";

import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";
import { Button } from "@/components/ui/button";
import { LOGIN_PATH } from "@/lib/config";

import { CurrentSessionBadge, TokenKindBadge, TokenStateBadge } from "./badges";
import { ConfirmAction } from "./confirm-action";
import { type AdminToken, memberHref, revokeToken } from "./data";
import { type Notice, useWriteFailure } from "./notice";
import { useAdminWrite } from "./use-admin-write";
import { When } from "./when";

const NARROW_HIDDEN = {
  id: "hidden sm:table-cell",
  host: "hidden sm:table-cell",
  created: "hidden 2xl:table-cell",
  lastUsed: "hidden lg:table-cell",
  expires: "hidden md:table-cell",
};

type Props = {
  tokens: AdminToken[];
  caption: string;
  /** False on a member's own page, where every row is theirs. */
  showLogin?: boolean;
  onNotice: (notice: Notice) => void;
  testId?: string;
};

/** Tokens and web sessions with their revoke action; the server's order (newest first) is kept. */
export function TokensTable({ tokens, caption, showLogin = true, onNotice, testId = "tokens-table" }: Props) {
  const t = useTranslations("admin.tokens");
  const tRevoke = useTranslations("admin.revokeToken");
  const failure = useWriteFailure();
  const write = useAdminWrite((api, id: number) => revokeToken(api, id));
  const [target, setTarget] = useState<AdminToken | null>(null);
  const [open, setOpen] = useState(false);
  const { reset } = write;
  const startRevoke = useCallback(
    (token: AdminToken) => {
      reset();
      setTarget(token);
      setOpen(true);
    },
    [reset],
  );

  const columns = useMemo(() => {
    const helper = dataTableColumns<AdminToken>();
    const login = showLogin
      ? [
          helper.accessor("login", {
            header: () => t("columns.login"),
            enableSorting: false,
            cell: (info) => (
              <Link href={memberHref(info.getValue())} className={NAME_LINK}>
                {info.getValue()}
              </Link>
            ),
          }),
        ]
      : [];
    return helper.columns([
      helper.accessor("id", {
        header: () => t("columns.id"),
        enableSorting: false,
        cell: (info) => <span className="font-mono text-[13px] tabular-nums">#{info.getValue()}</span>,
      }),
      ...login,
      helper.accessor("kind", {
        header: () => t("columns.kind"),
        enableSorting: false,
        cell: (info) => (
          <span className="flex flex-wrap items-center gap-1.5">
            <TokenKindBadge kind={info.getValue()} />
            {info.row.original.current ? <CurrentSessionBadge /> : null}
          </span>
        ),
      }),
      helper.accessor("host", {
        header: () => t("columns.host"),
        enableSorting: false,
        cell: (info) =>
          info.getValue() ? (
            <span className="block max-w-56 truncate font-mono text-xs text-foreground" title={info.getValue() ?? undefined}>
              {info.getValue()}
            </span>
          ) : (
            <span className="text-fg-subtle">{t("noHost")}</span>
          ),
      }),
      helper.accessor("created_at", {
        id: "created",
        header: () => t("columns.created"),
        enableSorting: false,
        meta: { numeric: true },
        cell: (info) => <When value={info.getValue()} short />,
      }),
      helper.accessor("last_used_at", {
        id: "lastUsed",
        header: () => t("columns.lastUsed"),
        enableSorting: false,
        meta: { numeric: true },
        cell: (info) => <When value={info.getValue()} never={t("never")} short />,
      }),
      helper.accessor("expires_at", {
        id: "expires",
        header: () => t("columns.expires"),
        enableSorting: false,
        meta: { numeric: true },
        cell: (info) => <When value={info.getValue()} short />,
      }),
      helper.accessor("state", {
        header: () => t("columns.state"),
        enableSorting: false,
        cell: (info) => <TokenStateBadge state={info.getValue()} />,
      }),
      helper.display({
        id: "actions",
        header: () => <span className="sr-only">{t("columns.actions")}</span>,
        meta: { actions: true },
        cell: (info) => {
          const token = info.row.original;
          if (token.state === "revoked") return null;
          return (
            <Button
              type="button"
              variant="quiet-danger"
              size="sm"
              aria-label={t("revokeLabel", { id: token.id, login: token.login })}
              onClick={() => startRevoke(token)}
              data-testid={`revoke-token-${token.id}`}
            >
              <KeyRound aria-hidden="true" />
              {t("revoke")}
            </Button>
          );
        },
      }),
    ]);
  }, [t, showLogin, startRevoke]);

  const confirm = async () => {
    if (!target) return;
    try {
      await write.mutateAsync(target.id); // resolves once the lists show the hub's new state
    } catch (error) {
      const failed = failure(error, { 404: tRevoke("notFound"), 409: tRevoke("conflict") });
      if (failed.status === 404 || failed.status === 409) {
        setOpen(false); // nothing left to do here: say so on the page, over the reloaded list
        onNotice({ tone: "error", text: failed.text });
      }
      return; // anything else is shown in the dialog, which stays open
    }
    setOpen(false);
    if (target.current) {
      window.location.assign(LOGIN_PATH); // the admin revoked the session this page runs on
      return;
    }
    onNotice({ tone: "success", text: tRevoke("success", { id: target.id, login: target.login }) });
  };

  const error = write.isError && open ? failure(write.error, { 404: tRevoke("notFound"), 409: tRevoke("conflict") }) : null;
  return (
    <>
      <DataTable
        data={tokens}
        columns={columns}
        caption={caption}
        getRowId={(row) => String(row.id)}
        columnClassNames={NARROW_HIDDEN}
        testId={testId}
      />
      <ConfirmAction
        open={open}
        onOpenChange={setOpen}
        title={target ? tRevoke("title", { id: target.id, login: target.login }) : ""}
        description={
          target ? (
            <>
              <p>
                {target.kind === "machine"
                  ? tRevoke("machine", { host: target.host ?? "" })
                  : target.kind === "worker"
                    ? tRevoke("worker", { host: target.host ?? "" })
                    : tRevoke("web")}
              </p>
              {target.current ? <p className="font-medium text-danger">{tRevoke("current")}</p> : null}
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
        testId="revoke-token-dialog"
      />
    </>
  );
}
