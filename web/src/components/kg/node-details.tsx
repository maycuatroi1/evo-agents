"use client";

import { ArrowLeft, ArrowRight, ExternalLink, FileText } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { Fragment, type ReactNode, useMemo, useState } from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { isWebLink } from "@/lib/kg/format";
import { groupRelations } from "@/lib/kg/graph";
import { nodeHref } from "@/lib/kg/routes";
import type { Evidence, KgNode, NodeDetail } from "@/lib/kg/types";
import { cn } from "@/lib/utils";

import { CopyButton, KindBadge, LabelBadge } from "./badges";

/** kg_node's edge reads stop at this many edges; a node with that many may have more. */
const EDGES_READ = 400;
const SHOWN_PROPS = new Set(["source", "uri"]);
const WEAK = new Set(["resolved", "proposed"]);

function Mono({ children }: { children: ReactNode }) {
  return <span className="font-mono text-xs [overflow-wrap:anywhere]">{children}</span>;
}

function Uri({ uri }: { uri: string }) {
  return isWebLink(uri) ? (
    <a
      href={uri}
      target="_blank"
      rel="noreferrer noopener"
      className="inline-flex items-start gap-1 font-mono text-xs break-all text-primary underline-offset-4 hover:underline"
      data-testid="node-uri"
    >
      {uri}
      <ExternalLink className="mt-0.5 size-3 shrink-0" aria-hidden="true" />
    </a>
  ) : (
    <Mono>{uri}</Mono>
  );
}

function propText(value: unknown): string {
  if (value === null || value === undefined) return "-";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

/** Label, source, uri, status and the other properties of the node. */
export function NodeFacts({ node, evidence }: { node: KgNode; evidence: Evidence[] }) {
  const t = useTranslations("kg.node");
  const format = useFormatter();
  const source = typeof node.props.source === "string" ? node.props.source : (evidence[0]?.source ?? null);
  const uri = typeof node.props.uri === "string" ? node.props.uri : (evidence[0]?.uri ?? null);
  const props = Object.entries(node.props).filter(([key]) => !SHOWN_PROPS.has(key));
  return (
    <Card data-testid="kg-node-facts">
      <CardHeader>
        <CardTitle>
          <h2>{t("facts")}</h2>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-1 gap-x-4 gap-y-2.5 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
          <dt className="text-muted-foreground">{t("id")}</dt>
          <dd className="flex min-w-0 items-start gap-1">
            <span data-testid="node-id">
              <Mono>{node.id}</Mono>
            </span>
            <CopyButton value={node.id} label={t("copyId")} />
          </dd>
          <dt className="text-muted-foreground">{t("kind")}</dt>
          <dd>
            <KindBadge kind={node.kind} />
          </dd>
          <dt className="text-muted-foreground">{t("label")}</dt>
          <dd className="flex flex-wrap items-center gap-2">
            <LabelBadge label={node.label} />
            <span className="text-xs text-muted-foreground">
              {t("labelParts", { location: node.label.location, integrity: node.label.integrity })}
            </span>
          </dd>
          <dt className="text-muted-foreground">{t("source")}</dt>
          <dd data-testid="node-source">{source ? <Mono>{source}</Mono> : "-"}</dd>
          <dt className="text-muted-foreground">{t("uri")}</dt>
          <dd>{uri ? <Uri uri={uri} /> : "-"}</dd>
          <dt className="text-muted-foreground">{t("status")}</dt>
          <dd className="flex flex-col gap-0.5">
            <Mono>{node.status}</Mono>
            {WEAK.has(node.status) ? <span className="text-xs text-muted-foreground">{t("weakStatus")}</span> : null}
          </dd>
          <dt className="text-muted-foreground">{t("confidence")}</dt>
          <dd className="tabular-nums">
            {node.conf === null ? "-" : format.number(node.conf, { style: "percent", maximumFractionDigits: 0 })}
          </dd>
          {node.aliases.length > 0 ? (
            <>
              <dt className="text-muted-foreground">{t("aliases")}</dt>
              <dd className="flex flex-col gap-0.5">
                {node.aliases.map((alias) => (
                  <Mono key={alias}>{alias}</Mono>
                ))}
              </dd>
            </>
          ) : null}
          {props.map(([key, value]) => (
            <Fragment key={key}>
              <dt className="font-mono text-xs text-muted-foreground sm:pt-0.5">{key}</dt>
              <dd>
                <Mono>{propText(value)}</Mono>
              </dd>
            </Fragment>
          ))}
        </dl>
      </CardContent>
    </Card>
  );
}

/** Where the node comes from: the items, revisions and spans it was read from. */
export function NodeEvidence({ project, node, evidence }: { project: string; node: KgNode; evidence: Evidence[] }) {
  const t = useTranslations("kg.evidence");
  return (
    <Card data-testid="kg-node-evidence">
      <CardHeader>
        <CardTitle>
          <h2>{t("title")}</h2>
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent>
        {evidence.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("empty")}</p>
        ) : (
          <ul className="flex flex-col gap-3">
            {evidence.map((ev, index) => (
              <li key={`${ev.item}:${ev.anchor ?? ""}:${index}`} className="flex flex-col gap-1 rounded-lg border px-3 py-2">
                <span className="flex items-start gap-1.5 text-sm">
                  <FileText className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                  {ev.item === node.id ? (
                    <Mono>{ev.anchor ? `${ev.item}#${ev.anchor}` : ev.item}</Mono>
                  ) : (
                    <Link href={nodeHref(project, ev.item)} className="text-primary underline-offset-4 hover:underline">
                      <Mono>{ev.anchor ? `${ev.item}#${ev.anchor}` : ev.item}</Mono>
                    </Link>
                  )}
                </span>
                <span className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
                  {ev.source ? <span>{t("source", { source: ev.source })}</span> : null}
                  {ev.rev ? <span>{t("rev", { rev: ev.rev })}</span> : null}
                  {ev.span ? <span>{t("span", { span: ev.span })}</span> : null}
                </span>
                {ev.uri ? <Uri uri={ev.uri} /> : null}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

/** Every visible neighbour of the node, grouped by edge type and direction, with a filter by group. */
export function NodeRelations({ project, detail }: { project: string; detail: NodeDetail }) {
  const t = useTranslations("kg.relations");
  const tn = useTranslations("kg.neighbours");
  const groups = useMemo(() => groupRelations(detail.outgoing, detail.incoming), [detail]);
  const [only, setOnly] = useState<string | null>(null);
  const shown = only ? groups.filter((group) => group.key === only) : groups;
  const total = detail.outgoing.length + detail.incoming.length;
  const caption = t("caption", { count: total });
  return (
    <section aria-labelledby="kg-relations" data-testid="kg-relations">
      <Card>
        <CardHeader>
          <CardTitle>
            <h2 id="kg-relations" tabIndex={-1} className="scroll-mt-20 outline-none">
              {t("title")}
            </h2>
          </CardTitle>
          <CardDescription>{t("description", { count: total })}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {total >= EDGES_READ ? <p className="text-xs text-muted-foreground">{t("capped", { count: EDGES_READ })}</p> : null}
          {groups.length === 0 ? (
            <p className="text-sm text-muted-foreground">{t("empty")}</p>
          ) : (
            <>
              <div role="group" aria-label={t("filter")} className="flex flex-wrap gap-1.5">
                <FilterChip pressed={only === null} onClick={() => setOnly(null)}>
                  {t("all", { count: total })}
                </FilterChip>
                {groups.map((group) => (
                  <FilterChip key={group.key} pressed={only === group.key} onClick={() => setOnly(only === group.key ? null : group.key)}>
                    {group.direction === "out" ? (
                      <ArrowRight className="size-3.5" aria-hidden="true" />
                    ) : (
                      <ArrowLeft className="size-3.5" aria-hidden="true" />
                    )}
                    <span className="sr-only">{tn(group.direction)} </span>
                    <span className="font-mono">{group.rel}</span>
                    <span className="text-muted-foreground tabular-nums">{group.relations.length}</span>
                  </FilterChip>
                ))}
              </div>
              <div className="overflow-hidden rounded-lg border">
                <Table scrollLabel={caption}>
                  <TableCaption className="sr-only">{caption}</TableCaption>
                  <TableHeader className="bg-muted/50">
                    <TableRow className="hover:bg-transparent">
                      <TableHead className="h-9 px-3 text-xs text-muted-foreground">{tn("rel")}</TableHead>
                      <TableHead className="h-9 px-3 text-xs text-muted-foreground">{tn("node")}</TableHead>
                      <TableHead className="hidden h-9 px-3 text-xs text-muted-foreground sm:table-cell">{tn("kind")}</TableHead>
                      <TableHead className="hidden h-9 px-3 text-xs text-muted-foreground md:table-cell">{t("status")}</TableHead>
                    </TableRow>
                  </TableHeader>
                  {shown.map((group) => (
                    <TableBody key={group.key} data-testid="relation-group" data-rel={group.rel} data-direction={group.direction}>
                      {group.relations.map((relation) => (
                        <TableRow key={`${group.key}:${relation.node}`} data-testid="relation-row" data-node-id={relation.node}>
                          <TableCell className="px-3 py-2">
                            <span className="inline-flex items-center gap-1 font-mono text-xs">
                              {group.direction === "out" ? (
                                <ArrowRight className="size-3.5" aria-hidden="true" />
                              ) : (
                                <ArrowLeft className="size-3.5" aria-hidden="true" />
                              )}
                              <span className="sr-only">{tn(group.direction)} </span>
                              {relation.rel}
                            </span>
                          </TableCell>
                          <TableCell className="px-3 py-2 whitespace-normal">
                            <Link
                              href={nodeHref(project, relation.node)}
                              className="font-medium break-words text-primary underline-offset-4 hover:underline"
                            >
                              {relation.name || relation.node}
                            </Link>
                            <span className="block font-mono text-[11px] break-all text-muted-foreground">{relation.node}</span>
                          </TableCell>
                          <TableCell className="hidden px-3 py-2 sm:table-cell">
                            <KindBadge kind={relation.kind} />
                          </TableCell>
                          <TableCell className="hidden px-3 py-2 font-mono text-xs md:table-cell">{relation.status}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  ))}
                </Table>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </section>
  );
}

function FilterChip({ pressed, onClick, children }: { pressed: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={onClick}
      className={cn(
        "inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-lg border px-2.5 text-xs font-medium transition-colors outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50",
        pressed ? "border-primary/40 bg-accent text-accent-foreground" : "bg-card hover:bg-muted",
      )}
    >
      {children}
    </button>
  );
}
