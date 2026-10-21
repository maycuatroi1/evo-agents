"use client";

import { useQuery } from "@tanstack/react-query";
import { Hash, MessageSquareQuote } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo } from "react";

import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK, Tag } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { type SignalKey, type SignalKind, signalsQuery } from "@/lib/signal-queries";
import { cn } from "@/lib/utils";

import { latestFilter } from "./model";

export function KindTag({ kind }: { kind: SignalKind }) {
  const t = useTranslations("signals.kind");
  const Icon = kind === "number" ? Hash : MessageSquareQuote;
  return (
    <Tag data-kind={kind}>
      <Icon aria-hidden="true" />
      {t(kind)}
    </Tag>
  );
}

/** A key's latest signal: its value with the key's unit (or the start of its feedback) and when it was measured. */
export function LatestValue({ project, signalKey }: { project: string; signalKey: SignalKey }) {
  const t = useTranslations("signals.keys");
  const format = useFormatter();
  const { data, isPending, isError } = useQuery(signalsQuery(browserApi, project, latestFilter(signalKey.name)));
  if (isPending) return <Skeleton className="h-4 w-24" />;
  if (isError) return <span className="text-xs text-fg-subtle">{t("latestFailed")}</span>;
  const [latest] = data;
  if (!latest) return <span className="text-xs text-fg-subtle">{t("noSignal")}</span>;
  return (
    <span className="flex min-w-0 flex-col items-end gap-0.5 text-right" data-testid="signal-latest" data-key={signalKey.name}>
      {latest.value !== null ? (
        <span className="font-medium text-foreground tabular-nums">
          {format.number(latest.value, { maximumFractionDigits: 4 })}
          {signalKey.unit ? <span className="ml-1 text-xs font-normal text-muted-foreground">{signalKey.unit}</span> : null}
        </span>
      ) : (
        <span className="max-w-56 truncate text-foreground" title={latest.text ?? ""}>
          {latest.text}
        </span>
      )}
      <span className="text-xs text-fg-subtle">
        <When value={latest.measured_at} short />
      </span>
    </span>
  );
}

const NARROW_HIDDEN = { kind: "hidden sm:table-cell", source: "hidden lg:table-cell", description: "hidden xl:table-cell xl:min-w-64" };

/**
 * The keys of the project's registry, by name: each key opens its series, with its kind, unit, source and what it
 * measures, and its latest value with when it was measured. The key shown below the table is the selected row.
 */
export function KeysTable({ project, keys, selected, hrefOf }: { project: string; keys: SignalKey[]; selected: string | null; hrefOf: (key: string) => Route }) {
  const t = useTranslations("signals.keys");
  const columns = useMemo(() => {
    const helper = dataTableColumns<SignalKey>();
    return helper.columns([
      helper.accessor("name", {
        header: () => t("columns.key"),
        sortFn: "alphanumeric",
        meta: { primary: true },
        cell: (info) => {
          const key = info.row.original;
          return (
            <CellMain sub={key.unit || undefined}>
              <Link
                href={hrefOf(key.name)}
                className={cn(NAME_LINK, "truncate font-mono text-xs", key.name === selected && "text-brand")}
                aria-current={key.name === selected ? "true" : undefined}
                data-key-link={key.name}
                scroll={false}
              >
                {key.name}
              </Link>
            </CellMain>
          );
        },
      }),
      helper.accessor("kind", { header: () => t("columns.kind"), cell: (info) => <KindTag kind={info.getValue()} /> }),
      helper.accessor("source", { header: () => t("columns.source"), cell: (info) => <span>{info.getValue() || "-"}</span> }),
      helper.accessor("description", {
        header: () => t("columns.description"),
        enableSorting: false,
        cell: (info) => (
          <span className="line-clamp-2 whitespace-normal" title={info.getValue()}>
            {info.getValue()}
          </span>
        ),
      }),
      helper.display({
        id: "latest",
        header: () => t("columns.latest"),
        meta: { numeric: true },
        cell: (info) => <LatestValue project={project} signalKey={info.row.original} />,
      }),
    ]);
  }, [t, project, selected, hrefOf]);
  return (
    <DataTable
      data={keys}
      columns={columns}
      caption={t("caption", { project })}
      getRowId={(row) => row.name}
      columnClassNames={NARROW_HIDDEN}
      testId="signal-keys-table"
      mobile={(key) => ({
        title: key.name,
        titleText: key.name,
        titleClassName: "font-mono",
        href: hrefOf(key.name),
        status: <LatestValue project={project} signalKey={key} />,
        meta: [key.kind === "number" ? key.unit || t("number") : t("feedback"), key.source].filter(Boolean).join(", "),
        data: { key: key.name },
      })}
    />
  );
}
