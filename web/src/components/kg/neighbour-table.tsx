"use client";

import { ArrowLeft, ArrowRight, ArrowUpRight, ChevronsDown, Crosshair } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { type Direction, displayName, type RowGroup } from "@/lib/kg/graph";
import { nodeHref } from "@/lib/kg/routes";
import type { GraphNode } from "@/lib/kg/types";

import { KindBadge } from "./badges";

function Arrow({ direction }: { direction: Direction }) {
  return direction === "out" ? (
    <ArrowRight className="size-3.5 text-muted-foreground" aria-hidden="true" />
  ) : (
    <ArrowLeft className="size-3.5 text-muted-foreground" aria-hidden="true" />
  );
}

/**
 * The accessible alternative to the canvas, always shown beside it (and alone on small screens): the edges of the
 * view that touch the selected node, with the node at the other end, in the groups the canvas draws (edge type,
 * direction, kind). A group of the node shown that holds more neighbours than it lists says so, and "show more"
 * opens it, here and in the canvas. Choosing a node here selects it in the canvas, and selecting one in the canvas
 * changes this table; "open" goes to that node's own page.
 */
export function NeighbourTable({
  project,
  node,
  focus,
  groups,
  onSelect,
  onOpenGroup,
  opening,
}: {
  project: string;
  node: GraphNode;
  focus: GraphNode;
  groups: RowGroup[];
  onSelect: (id: string) => void;
  onOpenGroup: (key: string) => void;
  /** The group being opened, while its answer is on its way. */
  opening: string | null;
}) {
  const t = useTranslations("kg.neighbours");
  const name = displayName(node);
  const count = groups.reduce((sum, group) => sum + group.rows.length, 0);
  const caption = t("caption", { name, count });
  return (
    <div className="flex min-w-0 flex-col gap-3" data-testid="kg-neighbours" data-selected={node.id}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 flex-col gap-0.5">
          <h3 className="text-[15px] leading-[22px] font-semibold break-words" id="kg-neighbours-title">
            {t("title", { name })}
          </h3>
          <p className="text-xs text-muted-foreground">
            {node.id === focus.id ? t("ofFocus", { count }) : t("ofSelected", { count })}
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
      {count === 0 && groups.every((group) => group.hidden === 0) ? (
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
            {groups.map((group) => (
              <TableBody key={group.key} data-testid="neighbour-group" data-key={group.key} data-hidden={group.hidden}>
                <TableRow className="bg-muted/30 hover:bg-muted/30">
                  <TableHead scope="rowgroup" colSpan={3} className="h-8 px-3 text-xs font-medium">
                    <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
                      <Arrow direction={group.direction} />
                      <span className="sr-only">{t(group.direction)} </span>
                      <span className="font-mono">{group.rel}</span>
                      <KindBadge kind={group.kind} />
                      <span className="font-normal text-muted-foreground tabular-nums">
                        {t("groupCount", { shown: group.rows.length, total: group.total })}
                      </span>
                    </span>
                  </TableHead>
                </TableRow>
                {group.rows.map((row) => {
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
                          <Arrow direction={row.direction} />
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
                            className="relative -my-1 inline-flex size-7 shrink-0 items-center justify-center rounded-sm text-muted-foreground transition-colors after:absolute after:-inset-2 hover:bg-muted hover:text-foreground md:after:hidden"
                            aria-label={t("open", { name: other })}
                            title={t("open", { name: other })}
                          >
                            <ArrowUpRight className="size-4" aria-hidden="true" />
                          </Link>
                        </span>
                        <span className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5">
                          <KindBadge kind={row.node.kind} />
                          <span className="font-mono text-[11px] [overflow-wrap:anywhere] text-muted-foreground">{row.node.id}</span>
                          <span className="text-[11px] text-muted-foreground tabular-nums">
                            {t("degree", { count: row.node.degree })}
                          </span>
                        </span>
                      </TableCell>
                      <TableCell className="hidden px-3 py-2 text-right align-top tabular-nums sm:table-cell">
                        {row.node.hop}
                      </TableCell>
                    </TableRow>
                  );
                })}
                {group.hidden > 0 ? (
                  <TableRow className="hover:bg-transparent">
                    <TableCell colSpan={3} className="px-3 py-1.5">
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="cursor-pointer"
                        disabled={opening !== null}
                        aria-busy={opening === group.key}
                        onClick={() => onOpenGroup(group.key)}
                        data-testid="neighbour-group-more"
                      >
                        <ChevronsDown aria-hidden="true" />
                        {t("showMore", { count: group.hidden, rel: group.rel, kind: group.kind })}
                      </Button>
                    </TableCell>
                  </TableRow>
                ) : null}
              </TableBody>
            ))}
          </Table>
        </div>
      )}
    </div>
  );
}
