"use client";

import { ArrowLeft, ArrowRight, ArrowUpRight, Crosshair } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { displayName, type NeighbourRow } from "@/lib/kg/graph";
import { nodeHref } from "@/lib/kg/routes";
import type { GraphNode } from "@/lib/kg/types";

import { KindBadge } from "./badges";

/**
 * The accessible alternative to the canvas, always shown beside it (and alone on small screens): the edges of the
 * view that touch the selected node, with the node at the other end. Choosing a node here selects it in the canvas,
 * and selecting one in the canvas changes this table; "open" goes to that node's own page.
 */
export function NeighbourTable({
  project,
  node,
  focus,
  rows,
  onSelect,
}: {
  project: string;
  node: GraphNode;
  focus: GraphNode;
  rows: NeighbourRow[];
  onSelect: (id: string) => void;
}) {
  const t = useTranslations("kg.neighbours");
  const name = displayName(node);
  const caption = t("caption", { name, count: rows.length });
  return (
    <div className="flex min-w-0 flex-col gap-3" data-testid="kg-neighbours" data-selected={node.id}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 flex-col gap-0.5">
          <h3 className="text-[15px] leading-[22px] font-semibold break-words" id="kg-neighbours-title">
            {t("title", { name })}
          </h3>
          <p className="text-xs text-muted-foreground">
            {node.id === focus.id ? t("ofFocus", { count: rows.length }) : t("ofSelected", { count: rows.length })}
          </p>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {node.id !== focus.id ? (
            <>
              <Button asChild variant="outline" size="sm">
                <Link href={nodeHref(project, node.id)} data-testid="kg-open-selected">
                  <ArrowUpRight aria-hidden="true" />
                  {t("openSelected")}
                </Link>
              </Button>
              <Button type="button" variant="ghost" size="sm" className="cursor-pointer" onClick={() => onSelect(focus.id)}>
                <Crosshair aria-hidden="true" />
                {t("backToFocus")}
              </Button>
            </>
          ) : null}
        </div>
      </div>
      {rows.length === 0 ? (
        <p className="rounded-md border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">{t("empty")}</p>
      ) : (
        <div className="overflow-hidden rounded-md border">
          <Table scrollLabel={caption}>
            <TableCaption className="sr-only">{caption}</TableCaption>
            <TableHeader className="bg-muted/50">
              <TableRow className="hover:bg-transparent">
                <TableHead className="h-9 px-3 text-xs text-muted-foreground">{t("rel")}</TableHead>
                <TableHead className="h-9 px-3 text-xs text-muted-foreground">{t("node")}</TableHead>
                <TableHead className="hidden h-9 px-3 text-right text-xs text-muted-foreground sm:table-cell">{t("hop")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => {
                const other = displayName(row.node);
                return (
                  <TableRow
                    key={`${row.edge.id}:${row.direction}`}
                    data-testid="neighbour-row"
                    data-node-id={row.node.id}
                    data-rel={row.edge.rel}
                    data-direction={row.direction}
                  >
                    <TableCell className="px-3 py-2 align-top">
                      <span className="inline-flex items-center gap-1 font-mono text-xs">
                        {row.direction === "out" ? (
                          <ArrowRight className="size-3.5 text-muted-foreground" aria-hidden="true" />
                        ) : (
                          <ArrowLeft className="size-3.5 text-muted-foreground" aria-hidden="true" />
                        )}
                        <span className="sr-only">{t(row.direction)} </span>
                        {row.edge.rel}
                      </span>
                    </TableCell>
                    <TableCell className="px-3 py-2 whitespace-normal">
                      <span className="flex items-start gap-1">
                        <button
                          type="button"
                          onClick={() => onSelect(row.node.id)}
                          className="cursor-pointer rounded-xs text-left font-medium break-words text-brand underline-offset-4 hover:underline"
                          aria-label={t("select", { name: other })}
                          data-testid="neighbour-select"
                        >
                          {other}
                        </button>
                        <Link
                          href={nodeHref(project, row.node.id)}
                          className="-my-1 inline-flex size-7 shrink-0 items-center justify-center rounded-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                          aria-label={t("open", { name: other })}
                          title={t("open", { name: other })}
                        >
                          <ArrowUpRight className="size-4" aria-hidden="true" />
                        </Link>
                      </span>
                      <span className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5">
                        <KindBadge kind={row.node.kind} />
                        <span className="font-mono text-[11px] [overflow-wrap:anywhere] text-muted-foreground">{row.node.id}</span>
                      </span>
                    </TableCell>
                    <TableCell className="hidden px-3 py-2 text-right align-top tabular-nums sm:table-cell">
                      {row.node.hop}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
