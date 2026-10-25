"use client";

import { BadgeCheck, Lock, RefreshCw, Store, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { DataCard } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { Tag } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { ConfirmAction } from "@/components/feedback/confirm-action";
import type { WriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { useMarketFailure, useRemoveTrust, useSourceAction } from "./data";
import { type MarketSource, marketSourcesQuery, type PluginPublisher, trustedPublishersQuery } from "./queries";
import { AddSourceDialog } from "./source-dialog";

const NARROW_HIDDEN = { plugins: "hidden sm:table-cell", fetched: "hidden md:table-cell" };

type Asked = { kind: "source"; source: MarketSource } | { kind: "trust"; id: number; publisher: string; signer: string };

function SourceName({ source }: { source: MarketSource }) {
  const t = useTranslations("market.admin");
  const sub = source.error ?? source.title ?? source.name ?? undefined;
  return (
    <CellMain sub={sub} subTitle={sub}>
      <span className="truncate font-mono text-[13px]" data-source-repo={source.repo}>
        {source.repo}
      </span>
      {source.builtin ? <Tag>{t("builtin")}</Tag> : null}
      {source.private ? (
        <Tag>
          <Lock aria-hidden="true" />
          {t("private")}
        </Tag>
      ) : null}
    </CellMain>
  );
}

function SourcesTable({ sources, onRemove }: { sources: MarketSource[]; onRemove: (source: MarketSource) => void }) {
  const t = useTranslations("market.admin");
  const columns = useMemo(() => {
    const helper = dataTableColumns<MarketSource>();
    return helper.columns([
      helper.accessor("repo", {
        header: () => t("columns.market"),
        meta: { primary: true },
        cell: (info) => <SourceName source={info.row.original} />,
      }),
      helper.accessor("plugins", {
        header: () => t("columns.plugins"),
        meta: { numeric: true },
        cell: (info) => <span className="tabular-nums">{info.getValue()}</span>,
      }),
      helper.accessor("fetched_at", {
        id: "fetched",
        header: () => t("columns.fetched"),
        meta: { numeric: true },
        cell: (info) => <When value={info.getValue()} never={t("never")} />,
      }),
      helper.display({
        id: "actions",
        header: () => <span className="sr-only">{t("columns.actions")}</span>,
        meta: { actions: true },
        cell: (info) => <SourceActions source={info.row.original} onRemove={onRemove} />,
      }),
    ]);
  }, [t, onRemove]);

  return (
    <DataTable
      data={sources}
      columns={columns}
      caption={t("sourcesCaption")}
      getRowId={(row) => String(row.id)}
      columnClassNames={NARROW_HIDDEN}
      testId="market-sources"
      mobile={(source) => ({
        title: source.repo,
        titleText: source.repo,
        titleClassName: "font-mono text-[13px]",
        tags: source.builtin ? <Tag>{t("builtin")}</Tag> : null,
        status: <span className="text-xs tabular-nums text-muted-foreground">{t("pluginCount", { count: source.plugins })}</span>,
        meta: source.error ?? source.title ?? source.name ?? undefined,
        danger: Boolean(source.error),
        actions: <SourceActions source={source} onRemove={onRemove} />,
        data: { "source-repo": source.repo },
      })}
    />
  );
}

/** Refresh a market now (its index and the latest release of each free plugin), or remove one that is not the operator's. */
function SourceActions({ source, onRemove }: { source: MarketSource; onRemove: (source: MarketSource) => void }) {
  const t = useTranslations("market.admin");
  const action = useSourceAction();
  const failure = useMarketFailure();
  const busy = action.isPending;
  const onRefresh = async () => {
    try {
      const done = await action.mutateAsync({ id: source.id, action: "refresh" });
      if ("plugins" in done && done.error) {
        notify({ tone: "error", text: t("refreshFailed", { repo: source.repo }), detail: done.error });
      } else {
        notify({ tone: "success", text: t("refreshed", { repo: source.repo, count: "plugins" in done ? done.plugins : 0 }) });
      }
    } catch (error) {
      notifyFailure(t("refreshFailed", { repo: source.repo }), failure(error));
    }
  };
  return (
    <div className="flex justify-end gap-1.5">
      <Button size="sm" variant="outline" busy={busy} onClick={() => void onRefresh()} data-testid="market-source-refresh">
        <RefreshCw aria-hidden="true" />
        <span className="max-sm:sr-only">{busy ? t("refreshing") : t("refresh")}</span>
      </Button>
      {source.builtin ? null : (
        <Button size="sm" variant="quiet-danger" onClick={() => onRemove(source)} aria-label={t("removeLabel", { repo: source.repo })} data-testid="market-source-remove">
          <Trash2 aria-hidden="true" />
        </Button>
      )}
    </div>
  );
}

function TrustedCard({ publishers, locked, onRemove }: { publishers: PluginPublisher[]; locked: boolean; onRemove: (asked: Asked) => void }) {
  const t = useTranslations("market.admin.trusted");
  const rows = publishers.flatMap((publisher) => (publisher.trusted ?? []).map((entry) => ({ publisher: publisher.id, ...entry })));
  return (
    <Card className="min-w-0 self-start" data-testid="market-trusted">
      <CardHeader>
        <CardTitle>
          <h2 className="flex items-center gap-2">
            <BadgeCheck className="size-4 text-muted-foreground" aria-hidden="true" />
            {t("title")}
          </h2>
        </CardTitle>
        <CardDescription>{locked ? t("locked") : t("description")}</CardDescription>
      </CardHeader>
      <CardContent className="min-w-0">
        {rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("none")}</p>
        ) : (
          <ul className="flex flex-col divide-y">
            {rows.map((row) => (
              <li key={row.id} className="flex min-w-0 items-start gap-3 py-2.5 first:pt-0 last:pb-0" data-trusted-publisher={row.publisher}>
                <div className="flex min-w-0 flex-1 flex-col gap-1">
                  <span className="font-mono text-xs font-medium">{row.publisher}</span>
                  <span className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">{row.identity}</span>
                  <span className="text-xs text-fg-subtle">
                    {row.added_by ? t("addedBy", { login: row.added_by }) : null} <When value={row.added_at} />
                  </span>
                </div>
                <Button
                  size="sm"
                  variant="quiet-danger"
                  onClick={() => onRemove({ kind: "trust", id: row.id, publisher: row.publisher, signer: row.identity })}
                  aria-label={t("removeLabel", { publisher: row.publisher })}
                  data-testid="market-trust-remove"
                >
                  <Trash2 aria-hidden="true" />
                </Button>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

/**
 * /admin/market, for hub admins (docs/market.md): the markets the hub reads, the operator's first and staying, each
 * with how many plugins its index lists, when the hub last read it and why it did not; Add a market, Refresh one now,
 * Remove one; and the publisher signers hub admins trusted from the market, each with who trusted it, to stop trusting.
 */
export function AdminMarketPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("market.admin");
  const state = useHubQuery(marketSourcesQuery(browserApi), initialError);
  const publishers = useHubQuery(trustedPublishersQuery(browserApi));
  const sourceAction = useSourceAction();
  const removeTrust = useRemoveTrust();
  const failure = useMarketFailure();
  const [asked, setAsked] = useState<Asked | null>(null);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const pending = sourceAction.isPending || removeTrust.isPending;

  const confirm = async () => {
    if (!asked) return;
    setRefused(null);
    try {
      if (asked.kind === "source") {
        await sourceAction.mutateAsync({ id: asked.source.id, action: "remove" });
        notify({ tone: "success", text: t("removed", { repo: asked.source.repo }) });
      } else {
        await removeTrust.mutateAsync(asked.id);
        notify({ tone: "success", text: t("trusted.removed", { publisher: asked.publisher }) });
      }
      setAsked(null);
    } catch (error) {
      setRefused(failure(error));
    }
  };

  return (
    <>
      <PageHeader title={t("title")} actions={<AddSourceDialog />} />
      <QueryView state={state} loading={<TableSkeleton rows={3} />}>
        {(held) => (
          <div className="grid min-w-0 gap-6 xl:grid-cols-[minmax(0,1fr)_24rem]">
            <div className="flex min-w-0 flex-col gap-4">
              {held.publishers_locked ? (
                <Alert data-testid="market-locked">
                  <Lock aria-hidden="true" />
                  <AlertDescription>{t("lockedNotice")}</AlertDescription>
                </Alert>
              ) : null}
              {held.sources.length === 0 ? (
                <EmptyState icon={Store} title={t("empty.title")} description={t("empty.description")} testId="market-sources-empty" />
              ) : (
                <DataCard>
                  <SourcesTable sources={held.sources} onRemove={(source) => setAsked({ kind: "source", source })} />
                </DataCard>
              )}
            </div>
            <TrustedCard
              publishers={publishers.status === "success" ? publishers.data : []}
              locked={held.publishers_locked}
              onRemove={setAsked}
            />
          </div>
        )}
      </QueryView>
      <ConfirmAction
        open={asked !== null}
        onOpenChange={(open) => {
          if (!open && !pending) {
            setAsked(null);
            setRefused(null);
          }
        }}
        title={
          asked?.kind === "source"
            ? t("removeTitle", { repo: asked.source.repo })
            : t("trusted.removeTitle", { publisher: asked?.kind === "trust" ? asked.publisher : "" })
        }
        description={<p>{asked?.kind === "source" ? t("removeDescription") : t("trusted.removeDescription")}</p>}
        confirmLabel={t("removeConfirm")}
        pendingLabel={t("removing")}
        cancelLabel={t("cancel")}
        pending={pending}
        error={refused}
        onConfirm={() => void confirm()}
        testId="market-remove"
      />
    </>
  );
}
