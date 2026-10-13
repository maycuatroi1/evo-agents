"use client";

import { Pencil, RotateCcw } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useMemo, useState } from "react";

import { DataCard } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { Tag } from "@/components/data/identifier";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";

import { useDeletePolicy, useFailure } from "./hooks";
import type { PolicyRow } from "./model";
import { PolicyDialog } from "./policy-dialog";

const NARROW_HIDDEN = { cooldown: "hidden md:table-cell", failover: "hidden sm:table-cell" };

function PolicyTable({ rows, onEdit, onReset }: { rows: PolicyRow[]; onEdit: (row: PolicyRow) => void; onReset: (row: PolicyRow) => void }) {
  const t = useTranslations("accounts.policies");
  const columns = useMemo(() => {
    const helper = dataTableColumns<PolicyRow>();
    return helper.columns([
      helper.accessor("project", {
        header: () => t("columns.project"),
        sortFn: "alphanumeric",
        meta: { primary: true },
        cell: (info) => (
          <CellMain>
            <span className="truncate font-mono text-[13px]" data-policy-project={info.getValue()}>
              {info.getValue()}
            </span>
            <Tag data-testid="policy-origin" data-custom={String(info.row.original.custom)}>
              {info.row.original.custom ? t("custom") : t("defaults")}
            </Tag>
          </CellMain>
        ),
      }),
      helper.accessor("strategy", {
        header: () => t("columns.strategy"),
        sortFn: "text",
        cell: (info) => (
          <span title={t(`strategyHint.${info.getValue()}`)} data-testid="policy-strategy-text">
            {t(`strategy.${info.getValue()}`)}
          </span>
        ),
      }),
      helper.accessor("cooldown_seconds", {
        id: "cooldown",
        header: () => t("columns.cooldown"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => <span data-testid="policy-cooldown-text">{t("minutes", { minutes: Math.round(info.getValue() / 60) })}</span>,
      }),
      helper.display({
        id: "failover",
        header: () => t("columns.failover"),
        cell: (info) => {
          const row = info.row.original;
          return (
            <span data-testid="policy-failover-text">{row.failover ? t("failoverOn", { count: row.max_failovers }) : t("failoverOff")}</span>
          );
        },
      }),
      helper.display({
        id: "actions",
        header: () => <span className="sr-only">{t("columns.actions")}</span>,
        meta: { actions: true },
        cell: (info) => {
          const row = info.row.original;
          return (
            <>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                aria-label={t("editLabel", { project: row.project })}
                onClick={() => onEdit(row)}
                data-testid="policy-edit"
              >
                <Pencil aria-hidden="true" />
                <span className="hidden sm:inline">{t("edit")}</span>
              </Button>
              {row.custom ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  aria-label={t("resetLabel", { project: row.project })}
                  onClick={() => onReset(row)}
                  data-testid="policy-reset"
                >
                  <RotateCcw aria-hidden="true" />
                  <span className="hidden sm:inline">{t("reset")}</span>
                </Button>
              ) : null}
            </>
          );
        },
      }),
    ]);
  }, [t, onEdit, onReset]);

  return (
    <DataTable
      data={rows}
      columns={columns}
      caption={t("caption")}
      getRowId={(row) => row.project}
      initialSorting={[{ id: "project", desc: false }]}
      columnClassNames={NARROW_HIDDEN}
      testId="policies-table"
    />
  );
}

/**
 * The visitor's rotation policy for each project their accounts are bound to: the defaults until they set one, Edit to
 * set it whole, Reset to drop it so the defaults hold again.
 */
export function PolicySection({ rows }: { rows: PolicyRow[] }) {
  const t = useTranslations("accounts.policies");
  const failureOf = useFailure();
  const reset = useDeletePolicy();
  const [editing, setEditing] = useState<{ open: boolean; policy: PolicyRow | null }>({ open: false, policy: null });
  const edit = useCallback((policy: PolicyRow) => setEditing({ open: true, policy }), []);
  const { mutateAsync } = reset;
  const drop = useCallback(
    async (row: PolicyRow) => {
      try {
        await mutateAsync(row.project);
      } catch (error) {
        notifyFailure(t("resetFailed", { project: row.project }), failureOf(error));
        return;
      }
      notify({ tone: "success", text: t("resetDone", { project: row.project }), description: t("resetText") });
    },
    [mutateAsync, failureOf, t],
  );
  const onReset = useCallback((row: PolicyRow) => void drop(row), [drop]);

  return (
    <section aria-labelledby="account-policies-title" className="flex flex-col gap-3" data-testid="account-policies">
      <div className="flex flex-col gap-1">
        <h2 id="account-policies-title" className="text-[15px] leading-[22px] font-semibold">
          {t("title")}
        </h2>
        <p className="text-sm text-pretty text-muted-foreground">{t("description")}</p>
      </div>
      {rows.length === 0 ? (
        <p className="rounded-md border border-dashed px-3 py-2.5 text-sm text-muted-foreground" data-testid="account-policies-empty">
          {t("empty")}
        </p>
      ) : (
        <DataCard>
          <PolicyTable rows={rows} onEdit={edit} onReset={onReset} />
        </DataCard>
      )}
      <PolicyDialog
        policy={editing.policy}
        open={editing.open}
        onOpenChange={(open) => setEditing((current) => ({ ...current, open }))}
      />
    </section>
  );
}
