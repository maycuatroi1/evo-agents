"use client";

import { ArrowLeft, ArrowRight, ArrowUpRight, EyeOff, ExternalLink, Expand, Minimize2, Telescope, X } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { Tag } from "@/components/data/identifier";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { isWebLink } from "@/lib/kg/format";
import { displayName, groupRelations, kindStyle, type RelationGroup } from "@/lib/kg/graph";
import { kgNodeQuery } from "@/lib/kg/queries";
import { clusterHref, nodeHref, reviewHref } from "@/lib/kg/routes";
import type { ClusterSummary, NodeDetail, Relation } from "@/lib/kg/types";
import { cn } from "@/lib/utils";

import { CopyButton, KindShape, LabelBadge } from "./badges";
import type { ClusterRow } from "./explorer-rail";

const TONE_BG = { 1: "bg-chart-1", 2: "bg-chart-2", 3: "bg-chart-3", 4: "bg-chart-4", 5: "bg-chart-5" } as const;
const RELATIONS_SHOWN = 5;
const PROPS_SHOWN = 6;
const HIDDEN_PROPS = new Set(["source", "uri", "deprecated"]);

/** The inspector's frame: what it is about, a close button, then its scrolling content. */
function Frame({ overline, onClose, children, testId }: { overline: string; onClose: () => void; children: ReactNode; testId: string }) {
  const t = useTranslations("kg.explorer.inspector");
  return (
    <aside aria-label={t("label")} className="flex min-h-0 flex-col" data-testid={testId}>
      <div className="flex h-11 shrink-0 items-center justify-between gap-2 border-b pr-2 pl-4">
        <p className="text-[11px] leading-4 font-semibold tracking-[0.06em] text-muted-foreground uppercase">{overline}</p>
        <Button type="button" variant="ghost" size="icon-sm" className="cursor-pointer max-md:size-11" aria-label={t("close")} title={t("close")} onClick={onClose}>
          <X aria-hidden="true" />
        </Button>
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-4">{children}</div>
    </aside>
  );
}

function Heading({ children, testId }: { children: ReactNode; testId?: string }) {
  return (
    <h2 className="text-[15px] leading-[22px] font-semibold [overflow-wrap:anywhere]" data-testid={testId}>
      {children}
    </h2>
  );
}

function SubTitle({ children }: { children: ReactNode }) {
  return <h3 className="text-xs font-medium text-muted-foreground">{children}</h3>;
}

/** The members of a cluster by kind: one bar in the kinds' tones, then the counts. */
function KindBreakdown({ kinds }: { kinds: Record<string, number> }) {
  const t = useTranslations("kg.explorer.inspector");
  const format = useFormatter();
  const rows = Object.entries(kinds).sort((a, b) => b[1] - a[1]);
  const total = rows.reduce((sum, [, count]) => sum + count, 0) || 1;
  return (
    <div className="flex flex-col gap-2">
      <SubTitle>{t("kinds")}</SubTitle>
      <div className="flex h-2 overflow-hidden rounded-full bg-muted" aria-hidden="true">
        {rows.map(([kind, count]) => (
          <span key={kind} className={cn("h-full", TONE_BG[kindStyle(kind).tone])} style={{ width: `${(count / total) * 100}%` }} />
        ))}
      </div>
      <ul className="flex flex-col gap-1 text-[13px]">
        {rows.map(([kind, count]) => (
          <li key={kind} className="flex items-center gap-2">
            <KindShape kind={kind} />
            <span className="min-w-0 flex-1 truncate">{kind}</span>
            <span className="text-xs text-muted-foreground tabular-nums">{format.number(count)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ClusterLinks({ title, rows, onPick, testId }: { title: string; rows: ClusterRow[]; onPick: (id: string) => void; testId: string }) {
  const t = useTranslations("kg.explorer.rail");
  if (rows.length === 0) return null;
  return (
    <div className="flex flex-col gap-1" data-testid={testId}>
      <SubTitle>{title}</SubTitle>
      <ul className="-mx-2 flex flex-col">
        {rows.slice(0, 8).map((row) => (
          <li key={row.id}>
            <button
              type="button"
              onClick={() => onPick(row.id)}
              className="flex w-full min-w-0 cursor-pointer items-center gap-2 rounded-sm px-2 py-1 text-left text-[13px] transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none max-md:min-h-11"
            >
              {row.colour ? <span className="size-2.5 shrink-0 rounded-xs" style={{ backgroundColor: row.colour }} aria-hidden="true" /> : null}
              <span className="min-w-0 flex-1 truncate" title={row.label}>
                {row.label}
              </span>
              <span className="text-xs text-muted-foreground tabular-nums">
                {row.weight !== undefined ? t("weight", { count: row.weight }) : row.size}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function ClusterInspector({
  project,
  cluster,
  colour,
  parentLabel,
  inScope,
  parts,
  linked,
  onExplore,
  onPickCluster,
  onClose,
}: {
  project: string;
  cluster: ClusterSummary;
  colour?: string;
  parentLabel?: string;
  /** Whether the canvas shows this cluster's members already. */
  inScope: boolean;
  parts: ClusterRow[];
  linked: ClusterRow[];
  onExplore: (id: string) => void;
  onPickCluster: (id: string) => void;
  onClose: () => void;
}) {
  const t = useTranslations("kg.explorer.inspector");
  const format = useFormatter();
  return (
    <Frame overline={cluster.level === 2 ? t("part") : t("cluster")} onClose={onClose} testId="kg-inspector-cluster">
      <div className="flex flex-col gap-1.5">
        <div className="flex items-start gap-2">
          {colour ? <span className="mt-1.5 size-3 shrink-0 rounded-xs" style={{ backgroundColor: colour }} aria-hidden="true" /> : null}
          <Heading testId="kg-inspector-title">{cluster.label}</Heading>
        </div>
        <p className="text-[13px] text-muted-foreground">
          {t("members", { count: cluster.size })}
          {cluster.subclusters > 0 ? `, ${t("partsCount", { count: cluster.subclusters })}` : ""}
          {parentLabel ? `, ${t("partOf", { parent: parentLabel })}` : ""}
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        {inScope ? null : (
          <Button type="button" size="sm" className="cursor-pointer" onClick={() => onExplore(cluster.id)} data-testid="kg-inspector-explore">
            <Telescope aria-hidden="true" />
            {t("exploreCluster")}
          </Button>
        )}
        <Button asChild variant="outline" size="sm">
          <Link href={clusterHref(project, cluster.id)}>
            <ArrowUpRight aria-hidden="true" />
            {t("clusterPage")}
          </Link>
        </Button>
        <Button asChild variant="outline" size="sm">
          <Link href={reviewHref(project, { cluster: cluster.level === 1 ? cluster.id : (cluster.parent ?? cluster.id) })}>{t("review")}</Link>
        </Button>
      </div>
      <KindBreakdown kinds={cluster.kinds} />
      <ClusterLinks title={t("parts")} rows={parts} onPick={onPickCluster} testId="kg-inspector-parts" />
      <ClusterLinks title={t("linked")} rows={linked} onPick={onPickCluster} testId="kg-inspector-linked" />
      <p className="text-xs text-fg-subtle">{t("clusterNote", { count: format.number(cluster.size) })}</p>
    </Frame>
  );
}

function propText(value: unknown): string {
  if (value === null || value === undefined) return "-";
  return typeof value === "string" ? value : JSON.stringify(value);
}

function Relations({ detail, onPick }: { detail: NodeDetail; onPick: (relation: Relation, direction: "out" | "in") => void }) {
  const t = useTranslations("kg.explorer.inspector");
  const groups = groupRelations(detail.outgoing, detail.incoming);
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  if (groups.length === 0) return <p className="text-xs text-muted-foreground">{t("noRelations")}</p>;
  return (
    <div className="flex flex-col gap-3" data-testid="kg-inspector-relations">
      <div className="flex flex-col gap-0.5">
        <SubTitle>{t("relations", { count: detail.outgoing.length + detail.incoming.length })}</SubTitle>
        <p className="text-xs text-fg-subtle">{t("relationsHint")}</p>
      </div>
      {groups.map((group: RelationGroup) => {
        const all = open.has(group.key);
        const shown = all ? group.relations : group.relations.slice(0, RELATIONS_SHOWN);
        const Arrow = group.direction === "out" ? ArrowRight : ArrowLeft;
        return (
          <div key={group.key} className="flex flex-col gap-0.5">
            <p className="flex items-center gap-1.5 font-mono text-xs text-muted-foreground">
              <Arrow className="size-3" aria-hidden="true" />
              <span className="sr-only">{group.direction === "out" ? t("outgoing") : t("incoming")}: </span>
              {group.rel}
              <span className="tabular-nums">({group.relations.length})</span>
            </p>
            <ul className="-mx-2 flex flex-col">
              {shown.map((relation) => (
                <li key={`${relation.node}\u0000${relation.rel}`}>
                  <button
                    type="button"
                    onClick={() => onPick(relation, group.direction)}
                    className="flex w-full min-w-0 cursor-pointer items-center gap-2 rounded-sm px-2 py-1 text-left text-[13px] transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none max-md:min-h-11"
                    data-testid="kg-relation"
                    data-node-id={relation.node}
                  >
                    <KindShape kind={relation.kind} />
                    <span className="min-w-0 flex-1 truncate" title={relation.node}>
                      {displayName({ id: relation.node, name: relation.name })}
                    </span>
                    <span className="sr-only">, {relation.kind}</span>
                  </button>
                </li>
              ))}
            </ul>
            {group.relations.length > RELATIONS_SHOWN ? (
              <Button
                type="button"
                variant="link"
                size="sm"
                className="h-auto w-fit p-0 text-xs"
                aria-expanded={all}
                onClick={() => setOpen(new Set(all ? [...open].filter((key) => key !== group.key) : [...open, group.key]))}
              >
                {all ? t("showFewer") : t("showAll", { count: group.relations.length })}
              </Button>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

export type NodeActions = {
  /** In a node's view: whether this node is expanded, is the node the view started from, and may expand now. */
  working?: { expanded: boolean; seed: boolean; canExpand: boolean };
  onExpand: (id: string) => void;
  onCollapse: (id: string) => void;
  onHide: (id: string) => void;
  onExplore: (id: string) => void;
  onRelation: (relation: Relation, direction: "out" | "in") => void;
  onClose: () => void;
};

function NodeBody({ project, detail, actions }: { project: string; detail: NodeDetail; actions: NodeActions }) {
  const t = useTranslations("kg.explorer.inspector");
  const { node } = detail;
  const source = typeof node.props.source === "string" ? node.props.source : (detail.evidence[0]?.source ?? null);
  const uri = typeof node.props.uri === "string" ? node.props.uri : (detail.evidence[0]?.uri ?? null);
  const props = Object.entries(node.props).filter(([key]) => !HIDDEN_PROPS.has(key)).slice(0, PROPS_SHOWN);
  const working = actions.working;
  return (
    <>
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <Tag>
            <KindShape kind={node.kind} className="size-3" />
            {node.kind}
          </Tag>
          <LabelBadge label={node.label} />
          <span className="rounded-full border px-2 py-0.5 font-mono text-[11px] text-muted-foreground" title={t("status")}>
            <span className="sr-only">{t("status")}: </span>
            {node.status}
          </span>
        </div>
        <Heading testId="kg-inspector-title">{displayName(node)}</Heading>
        <p className="flex min-w-0 items-start gap-1">
          <span className="min-w-0 font-mono text-xs [overflow-wrap:anywhere] text-muted-foreground" data-testid="kg-inspector-id">
            {node.id}
          </span>
          <CopyButton value={node.id} label={t("copyId")} />
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        {working ? (
          working.expanded && !working.seed ? (
            <Button type="button" size="sm" className="cursor-pointer" onClick={() => actions.onCollapse(node.id)} data-testid="kg-inspector-collapse">
              <Minimize2 aria-hidden="true" />
              {t("collapse")}
            </Button>
          ) : working.expanded ? null : (
            <Button
              type="button"
              size="sm"
              className="cursor-pointer"
              disabled={!working.canExpand}
              onClick={() => actions.onExpand(node.id)}
              data-testid="kg-inspector-expand"
            >
              <Expand aria-hidden="true" />
              {t("expand")}
            </Button>
          )
        ) : (
          <Button type="button" size="sm" className="cursor-pointer" onClick={() => actions.onExplore(node.id)} data-testid="kg-inspector-explore">
            <Telescope aria-hidden="true" />
            {t("exploreHere")}
          </Button>
        )}
        {working && !working.seed ? (
          <>
            <Button type="button" variant="outline" size="sm" className="cursor-pointer" onClick={() => actions.onExplore(node.id)}>
              <Telescope aria-hidden="true" />
              {t("exploreHere")}
            </Button>
            <Button type="button" variant="outline" size="sm" className="cursor-pointer" onClick={() => actions.onHide(node.id)} data-testid="kg-inspector-hide">
              <EyeOff aria-hidden="true" />
              {t("hide")}
            </Button>
          </>
        ) : null}
        <Button asChild variant="outline" size="sm">
          <Link href={nodeHref(project, node.id)} data-testid="kg-inspector-node-page">
            <ArrowUpRight aria-hidden="true" />
            {t("nodePage")}
          </Link>
        </Button>
      </div>
      {source || uri || props.length > 0 ? (
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-[13px]">
          {source ? (
            <>
              <dt className="text-muted-foreground">{t("source")}</dt>
              <dd className="font-mono text-xs [overflow-wrap:anywhere]">{source}</dd>
            </>
          ) : null}
          {uri ? (
            <>
              <dt className="text-muted-foreground">{t("uri")}</dt>
              <dd className="min-w-0">
                {isWebLink(uri) ? (
                  <a href={uri} target="_blank" rel="noreferrer noopener" className="inline-flex items-start gap-1 font-mono text-xs break-all text-brand underline-offset-4 hover:underline">
                    {uri}
                    <ExternalLink className="mt-0.5 size-3 shrink-0" aria-hidden="true" />
                  </a>
                ) : (
                  <span className="font-mono text-xs [overflow-wrap:anywhere]">{uri}</span>
                )}
              </dd>
            </>
          ) : null}
          {props.map(([key, value]) => (
            <div key={key} className="contents">
              <dt className="font-mono text-xs text-muted-foreground">{key}</dt>
              <dd className="line-clamp-3 font-mono text-xs [overflow-wrap:anywhere]">{propText(value)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      <Relations detail={detail} onPick={actions.onRelation} />
    </>
  );
}

export function NodeInspector({ project, id, actions }: { project: string; id: string; actions: NodeActions }) {
  const t = useTranslations("kg.explorer.inspector");
  const state = useHubQuery(kgNodeQuery(browserApi, project, id), null);
  return (
    <Frame overline={t("node")} onClose={actions.onClose} testId="kg-inspector-node">
      {state.status === "error" && state.error.status === 404 ? (
        <p className="text-sm text-muted-foreground" data-testid="kg-inspector-missing">
          {t("notFound")}
        </p>
      ) : (
        <QueryView
          state={state}
          loading={
            <div className="flex flex-col gap-3" aria-label={t("loading")}>
              <Skeleton className="h-5 w-40" />
              <Skeleton className="h-4 w-full" />
              <Skeleton className="h-24 w-full" />
            </div>
          }
        >
          {(detail) => <NodeBody project={project} detail={detail} actions={actions} />}
        </QueryView>
      )}
    </Frame>
  );
}
