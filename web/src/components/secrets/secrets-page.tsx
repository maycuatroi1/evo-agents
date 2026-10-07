"use client";

import { CalendarX, GitBranch, LockKeyhole, Plus, RefreshCw, Trash2, Variable } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useState } from "react";

import { ConfirmAction } from "@/components/admin/confirm-action";
import { NoticeArea, type Notice, useNotice } from "@/components/admin/notice";
import { When } from "@/components/admin/when";
import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { useNow } from "@/components/kg/use-now";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, PageSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ChipList } from "@/components/workers/badges";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { useDeleteSecret, useSecretFailure } from "./hooks";
import { isExpired } from "./model";
import { type Secret, type SecretKind, secretsQuery } from "./queries";
import { SecretDialog } from "./secret-dialog";

const NARROW_HIDDEN = {
  kind: "hidden sm:table-cell",
  projects: "hidden md:table-cell",
  workers: "hidden lg:table-cell",
  expires: "hidden sm:table-cell",
  updated: "hidden xl:table-cell",
};

const KIND_ICONS: Record<SecretKind, typeof Variable> = { env: Variable, git: GitBranch };

/** A secret's kind as an icon and a word. */
export function SecretKindBadge({ kind }: { kind: SecretKind }) {
  const t = useTranslations("secrets.kind");
  const Icon = KIND_ICONS[kind];
  return (
    <Badge variant="secondary" data-kind={kind} data-testid="secret-kind-badge">
      <Icon aria-hidden="true" />
      {t(kind)}
    </Badge>
  );
}

/** What the secret is for: the variable it sets, or the origins it answers for and the user git sends. */
function Target({ secret }: { secret: Secret }) {
  const t = useTranslations("secrets");
  if (secret.kind === "env") {
    return <span className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">{secret.env_var}</span>;
  }
  return (
    <span className="text-xs text-muted-foreground [overflow-wrap:anywhere]">
      {t.rich("gitTarget", {
        prefix: secret.url_prefix ?? "",
        user: secret.username ?? "",
        code: (chunks) => <span className="font-mono">{chunks}</span>,
      })}
    </span>
  );
}

/** When the secret ends, and whether it has: read against the browser's clock once the page hydrated. */
function Expiry({ secret }: { secret: Secret }) {
  const t = useTranslations("secrets");
  const now = useNow(secret.expires_at !== null);
  if (!secret.expires_at) return <span className="text-xs text-muted-foreground">{t("never")}</span>;
  const expired = now !== null && isExpired(secret, now);
  return (
    <span className="flex flex-col items-start gap-1">
      <When value={secret.expires_at} short />
      {expired ? (
        <Badge variant="warning" data-testid="secret-expired">
          <CalendarX aria-hidden="true" />
          {t("expired")}
        </Badge>
      ) : null}
    </span>
  );
}

function SecretsTable({
  secrets,
  onReplace,
  onDelete,
}: {
  secrets: Secret[];
  onReplace: (secret: Secret) => void;
  onDelete: (secret: Secret) => void;
}) {
  const t = useTranslations("secrets");
  const columns = useMemo(() => {
    const helper = dataTableColumns<Secret>();
    return helper.columns([
      helper.accessor("name", {
        header: () => t("columns.secret"),
        sortFn: "alphanumeric",
        cell: (info) => {
          const secret = info.row.original;
          return (
            <div className="flex min-w-36 flex-col gap-0.5 py-0.5 whitespace-normal sm:min-w-48" data-secret-name={secret.name}>
              <span className="font-mono text-sm font-medium [overflow-wrap:anywhere]">{secret.name}</span>
              <Target secret={secret} />
            </div>
          );
        },
      }),
      helper.accessor("kind", {
        header: () => t("columns.kind"),
        sortFn: "text",
        cell: (info) => <SecretKindBadge kind={info.getValue()} />,
      }),
      helper.display({
        id: "projects",
        header: () => t("columns.projects"),
        cell: (info) => <ChipList items={info.row.original.projects} empty="-" compact />,
      }),
      helper.display({
        id: "workers",
        header: () => t("columns.workers"),
        cell: (info) => <ChipList items={info.row.original.workers} empty={t("anyWorker")} compact />,
      }),
      helper.accessor((row) => (row.expires_at ? new Date(row.expires_at) : new Date(8.64e15)), {
        id: "expires",
        header: () => t("columns.expires"),
        sortFn: "datetime",
        cell: (info) => <Expiry secret={info.row.original} />,
      }),
      helper.accessor((row) => new Date(row.updated_at), {
        id: "updated",
        header: () => t("columns.updated"),
        sortFn: "datetime",
        cell: (info) => <When value={info.row.original.updated_at} short />,
      }),
      helper.display({
        id: "actions",
        header: () => <span className="sr-only">{t("columns.actions")}</span>,
        cell: (info) => {
          const secret = info.row.original;
          return (
            <div className="flex flex-nowrap justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                size="lg"
                aria-label={t("replaceLabel", { name: secret.name })}
                onClick={() => onReplace(secret)}
                data-testid="secret-replace"
              >
                <RefreshCw aria-hidden="true" />
                <span className="hidden sm:inline">{t("replace")}</span>
              </Button>
              <Button
                type="button"
                variant="outline"
                size="lg"
                className="text-destructive hover:text-destructive"
                aria-label={t("deleteLabel", { name: secret.name })}
                onClick={() => onDelete(secret)}
                data-testid="secret-delete"
              >
                <Trash2 aria-hidden="true" />
                <span className="hidden sm:inline">{t("delete")}</span>
              </Button>
            </div>
          );
        },
      }),
    ]);
  }, [t, onReplace, onDelete]);

  return (
    <DataTable
      data={secrets}
      columns={columns}
      caption={t("caption")}
      getRowId={(row) => row.name}
      initialSorting={[{ id: "name", desc: false }]}
      columnClassNames={NARROW_HIDDEN}
      testId="secrets-table"
    />
  );
}

function DeleteSecret({
  secret,
  open,
  onOpenChange,
  onNotice,
}: {
  secret: Secret | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onNotice: (notice: Notice) => void;
}) {
  const t = useTranslations("secrets.deleteDialog");
  const failure = useSecretFailure();
  const write = useDeleteSecret();
  const { reset } = write;
  useEffect(() => {
    if (open) reset();
  }, [open, reset]);

  const confirm = async () => {
    if (!secret) return;
    try {
      await write.mutateAsync(secret.name); // resolves once the list shows the hub's new state
    } catch (error) {
      const failed = failure(error, { 404: t("notFound") });
      if (failed.status === 404) {
        onOpenChange(false); // nothing left to delete: say so on the page, over the reloaded list
        onNotice({ tone: "error", text: failed.text });
      }
      return; // anything else is shown in the dialog, which stays open
    }
    onOpenChange(false);
    onNotice({ tone: "success", text: t("success", { name: secret.name }) });
  };

  const error = write.isError && open ? failure(write.error, { 404: t("notFound") }) : null;
  return (
    <ConfirmAction
      open={open}
      onOpenChange={onOpenChange}
      title={secret ? t("title", { name: secret.name }) : ""}
      description={
        <>
          <p>{t("leases")}</p>
          <p>{t("source")}</p>
          <p>{t("audit")}</p>
        </>
      }
      confirmLabel={t("confirm")}
      pendingLabel={t("pending")}
      cancelLabel={t("cancel")}
      pending={write.isPending}
      error={error}
      onConfirm={() => void confirm()}
      testId="delete-secret-dialog"
    />
  );
}

/**
 * The visitor's secrets: what each one is for, where it goes and when it ends, never its value. Add opens an empty
 * form; Replace opens the form of a secret without its value, which the visitor types again; Delete asks first.
 */
export function SecretsPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("secrets");
  const state = useHubQuery(secretsQuery(browserApi), initialError);
  const { notice, show, clear } = useNotice();
  const [editing, setEditing] = useState<{ open: boolean; secret: Secret | null }>({ open: false, secret: null });
  const [deleting, setDeleting] = useState<{ open: boolean; secret: Secret | null }>({ open: false, secret: null });
  const secrets = state.status === "success" ? state.data : null;
  const names = useMemo(() => (secrets ?? []).map((secret) => secret.name), [secrets]);

  const add = () => setEditing({ open: true, secret: null });
  const replace = useCallback((secret: Secret) => setEditing({ open: true, secret }), []);
  const remove = useCallback((secret: Secret) => setDeleting({ open: true, secret }), []);

  return (
    <>
      <PageHeader
        eyebrow={t("eyebrow")}
        title={t("title")}
        description={t("description")}
        meta={
          <>
            {secrets ? <Badge variant="secondary">{t("count", { count: secrets.length })}</Badge> : null}
            <Button size="lg" onClick={add} data-testid="secrets-add">
              <Plus aria-hidden="true" />
              {t("addButton")}
            </Button>
          </>
        }
      />
      <NoticeArea notice={notice} onDismiss={clear} />
      <QueryView state={state} loading={<PageSkeleton />}>
        {(list) =>
          list.length === 0 ? (
            <EmptyState
              icon={LockKeyhole}
              title={t("empty.title")}
              description={t.rich("empty.description", {
                code: (chunks) => <code className="rounded bg-muted px-1 font-mono text-xs text-foreground">{chunks}</code>,
              })}
            >
              <Button size="lg" onClick={add} data-testid="secrets-empty-add">
                <Plus aria-hidden="true" />
                {t("addButton")}
              </Button>
            </EmptyState>
          ) : (
            <section aria-labelledby="secrets-list-title" className="flex flex-col gap-3">
              <h2 id="secrets-list-title" className="sr-only">
                {t("caption")}
              </h2>
              <p className="text-sm text-pretty text-muted-foreground" data-testid="secrets-list-summary">
                {t("listSummary", { count: list.length })}
              </p>
              <SecretsTable secrets={list} onReplace={replace} onDelete={remove} />
            </section>
          )
        }
      </QueryView>
      <SecretDialog
        open={editing.open}
        onOpenChange={(open) => setEditing((current) => ({ ...current, open }))}
        secret={editing.secret}
        taken={names}
        onSaved={show}
      />
      <DeleteSecret
        secret={deleting.secret}
        open={deleting.open}
        onOpenChange={(open) => setDeleting((current) => ({ ...current, open }))}
        onNotice={show}
      />
    </>
  );
}
