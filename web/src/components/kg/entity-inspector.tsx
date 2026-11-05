"use client";

import { ArrowUpRight, Expand, ExternalLink, FileText, Telescope } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { Tag } from "@/components/data/identifier";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { type Citation, edgeLabel, type EntityDetail, type EntityEdge, type RelationDetail } from "@/lib/kg/entity-graph";
import { entityDetailQuery, relationDetailQuery } from "@/lib/kg/entity-queries";
import { nodeHref } from "@/lib/kg/routes";
import type { NodeLabel } from "@/lib/kg/types";

import { LabelBadge } from "./badges";
import { citationHref, CodeLinks } from "./entity-code";
import { TypeDot } from "./entity-rail";
import { Frame, Heading, SubTitle } from "./explorer-inspector";

const CITATIONS_SHOWN = 4;
const RELATIONS_SHOWN = 6;

export type EntityActions = {
  onClose: () => void;
  onSelectNode: (id: string) => void;
  onSelectEdge: (id: string) => void;
  onExpand: (id: string) => void;
  onFocus: (id: string) => void;
  /** Whether the entity's neighbours are open already. */
  expanded: (id: string) => boolean;
};

function Loading() {
  const t = useTranslations("kg.explorer.entities.inspector");
  return (
    <div className="flex flex-col gap-3" aria-label={t("loading")}>
      <Skeleton className="h-5 w-40" />
      <Skeleton className="h-12 w-full" />
      <Skeleton className="h-24 w-full" />
    </div>
  );
}

/** A thing the model stated: its status, model, confidence and label, so a reader weighs it before trusting it. */
function Provenance({ status, model, conf, label }: { status: string; model: string | null; conf: number | null; label: NodeLabel }) {
  const t = useTranslations("kg.explorer.entities.inspector");
  const format = useFormatter();
  return (
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-[13px]" data-testid="kg-entity-provenance">
      <dt className="text-muted-foreground">{t("status")}</dt>
      <dd>
        <span className="rounded-full border px-2 py-0.5 font-mono text-[11px]">{status}</span>
      </dd>
      <dt className="text-muted-foreground">{t("model")}</dt>
      <dd className="font-mono text-xs [overflow-wrap:anywhere]">{model ?? t("noModel")}</dd>
      {conf === null ? null : (
        <>
          <dt className="text-muted-foreground">{t("confidence")}</dt>
          <dd className="tabular-nums">{format.number(conf, { style: "percent", maximumFractionDigits: 0 })}</dd>
        </>
      )}
      <dt className="text-muted-foreground">{t("label")}</dt>
      <dd>
        <LabelBadge label={label} />
      </dd>
    </dl>
  );
}

function Citations({ project, citations, total }: { project: string; citations: Citation[]; total: number }) {
  const t = useTranslations("kg.explorer.entities.inspector");
  const [all, setAll] = useState(false);
  const shown = all ? citations : citations.slice(0, CITATIONS_SHOWN);
  return (
    <section className="flex flex-col gap-2" data-testid="kg-entity-citations">
      <SubTitle>{t("citations", { count: total })}</SubTitle>
      {citations.length === 0 ? (
        <p className="text-xs text-muted-foreground">{t("noCitations")}</p>
      ) : (
        <ol className="flex flex-col gap-2">
          {shown.map((citation, index) => {
            const href = citationHref(citation);
            return (
              <li key={`${citation.document}:${index}`} className="flex flex-col gap-1 rounded-md border bg-muted/40 p-2.5" data-testid="kg-entity-citation">
                <div className="flex min-w-0 items-start gap-1.5">
                  <FileText className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                  <Link
                    href={nodeHref(project, citation.document)}
                    className="min-w-0 text-[13px] font-medium [overflow-wrap:anywhere] text-foreground underline-offset-4 hover:underline"
                    title={citation.path ?? citation.name}
                  >
                    {citation.name}
                  </Link>
                  {citation.line ? <span className="shrink-0 font-mono text-[11px] text-fg-subtle">{t("line", { line: citation.line })}</span> : null}
                </div>
                {citation.quote ? (
                  <blockquote className="border-l-2 pl-2 text-xs leading-5 text-muted-foreground [overflow-wrap:anywhere]">“{citation.quote}”</blockquote>
                ) : null}
                {citation.description ? <p className="text-xs leading-5 [overflow-wrap:anywhere]">{citation.description}</p> : null}
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-fg-subtle">
                  {citation.model ? <span className="font-mono">{citation.model}</span> : null}
                  {href ? (
                    <a
                      href={href}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="inline-flex cursor-pointer items-center gap-1 text-brand underline-offset-4 hover:underline max-md:min-h-11"
                      data-testid="kg-entity-citation-source"
                    >
                      {t("openSource")}
                      <ExternalLink className="size-3" aria-hidden="true" />
                    </a>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ol>
      )}
      {citations.length > CITATIONS_SHOWN ? (
        <Button type="button" variant="ghost" size="sm" className="w-fit cursor-pointer" onClick={() => setAll(!all)}>
          {all ? t("showFewer") : t("showAll", { count: citations.length })}
        </Button>
      ) : null}
    </section>
  );
}

function Keywords({ text }: { text: string | null }) {
  const words = (text ?? "").split(/[,;]/).map((word) => word.trim()).filter(Boolean);
  if (words.length === 0) return null;
  return (
    <ul className="flex flex-wrap gap-1" data-testid="kg-relation-keywords">
      {words.map((word) => (
        <li key={word} className="rounded-full bg-muted px-2 py-0.5 text-[11px] text-foreground">
          {word}
        </li>
      ))}
    </ul>
  );
}

function Figures({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="grid divide-x rounded-md border" style={{ gridTemplateColumns: `repeat(${rows.length}, minmax(0, 1fr))` }}>
      {rows.map(([label, value]) => (
        <div key={label} className="flex min-w-0 flex-col gap-0.5 px-2.5 py-2">
          <dt className="truncate text-xs text-muted-foreground">{label}</dt>
          <dd className="truncate text-[15px] leading-[22px] font-semibold tabular-nums">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function RelationList({ detail, actions }: { detail: EntityDetail; actions: EntityActions }) {
  const t = useTranslations("kg.explorer.entities.inspector");
  const format = useFormatter();
  const [all, setAll] = useState(false);
  const names = new Map(detail.neighbours.map((node) => [node.id, node.name]));
  const shown = all ? detail.relations : detail.relations.slice(0, RELATIONS_SHOWN);
  const other = (edge: EntityEdge) => (edge.src === detail.node.id ? edge.dst : edge.src);
  return (
    <section className="flex flex-col gap-2" data-testid="kg-entity-relations">
      <SubTitle>{t("relations", { count: detail.relations.length })}</SubTitle>
      {detail.relations.length === 0 ? (
        <p className="text-xs text-muted-foreground">{t("noRelations")}</p>
      ) : (
        <ul className="-mx-2 flex flex-col">
          {shown.map((edge) => (
            <li key={edge.id}>
              <button
                type="button"
                onClick={() => actions.onSelectEdge(edge.id)}
                className="flex w-full min-w-0 cursor-pointer flex-col gap-0.5 rounded-sm px-2 py-1.5 text-left transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none max-md:min-h-11"
                data-testid="kg-entity-relation"
              >
                <span className="flex min-w-0 items-center justify-between gap-2 text-[13px]">
                  <span className="truncate font-medium">{names.get(other(edge)) ?? other(edge)}</span>
                  <span className="shrink-0 text-[11px] text-fg-subtle tabular-nums" title={t("weight")}>
                    {format.number(edge.weight, { maximumFractionDigits: 1 })}
                  </span>
                </span>
                <span className="line-clamp-2 text-xs text-muted-foreground">{edgeLabel(edge)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {detail.relations.length > RELATIONS_SHOWN ? (
        <Button type="button" variant="ghost" size="sm" className="w-fit cursor-pointer" onClick={() => setAll(!all)}>
          {all ? t("showFewer") : t("showAll", { count: detail.relations.length })}
        </Button>
      ) : null}
    </section>
  );
}

function EntityBody({ project, detail, actions }: { project: string; detail: EntityDetail; actions: EntityActions }) {
  const t = useTranslations("kg.explorer.entities.inspector");
  const format = useFormatter();
  const { node } = detail;
  return (
    <>
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <Tag>
            <TypeDot kind={node.kind} />
            {node.kind}
          </Tag>
          {node.type && node.type !== node.kind ? <Tag title={t("modelType")}>{node.type}</Tag> : null}
        </div>
        <Heading testId="kg-entity-title">{node.name}</Heading>
        {detail.aliases.length > 0 ? (
          <p className="text-xs text-muted-foreground [overflow-wrap:anywhere]">{t("aliases", { names: detail.aliases.join(", ") })}</p>
        ) : null}
      </div>
      <p className="text-[13px] leading-5 [overflow-wrap:anywhere]" data-testid="kg-entity-description">
        {node.description ?? <span className="text-muted-foreground">{t("noDescription")}</span>}
      </p>
      <div className="flex flex-wrap gap-2">
        <Button type="button" size="sm" className="cursor-pointer" disabled={actions.expanded(node.id)} onClick={() => actions.onExpand(node.id)} data-testid="kg-entity-expand">
          <Expand aria-hidden="true" />
          {actions.expanded(node.id) ? t("expanded") : t("expand")}
        </Button>
        <Button type="button" variant="outline" size="sm" className="cursor-pointer" onClick={() => actions.onFocus(node.id)} data-testid="kg-entity-focus">
          <Telescope aria-hidden="true" />
          {t("focus")}
        </Button>
        <Button asChild variant="outline" size="sm">
          <Link href={nodeHref(project, node.id)}>
            <ArrowUpRight aria-hidden="true" />
            {t("nodePage")}
          </Link>
        </Button>
      </div>
      <Figures
        rows={[
          [t("documents"), format.number(detail.documents)],
          [t("relationsShort"), format.number(node.degree)],
          [t("neighbours"), format.number(detail.neighbours.length)],
          [t("codeShort"), format.number(detail.code_links ?? 0)],
        ]}
      />
      <Provenance status={node.status} model={node.model} conf={node.conf} label={node.label} />
      <CodeLinks project={project} links={detail.code ?? []} total={detail.code_links ?? 0} />
      <Citations project={project} citations={detail.citations} total={detail.documents} />
      <RelationList detail={detail} actions={actions} />
    </>
  );
}

export function EntityInspector({ project, id, actions }: { project: string; id: string; actions: EntityActions }) {
  const t = useTranslations("kg.explorer.entities.inspector");
  const state = useHubQuery(entityDetailQuery(browserApi, project, id), null);
  return (
    <Frame overline={t("entity")} onClose={actions.onClose} testId="kg-inspector-entity">
      {state.status === "error" && state.error.status === 404 ? (
        <p className="text-sm text-muted-foreground">{t("entityNotFound")}</p>
      ) : (
        <QueryView state={state} loading={<Loading />}>
          {(detail) => <EntityBody project={project} detail={detail} actions={actions} />}
        </QueryView>
      )}
    </Frame>
  );
}

function RelationBody({ project, detail, actions }: { project: string; detail: RelationDetail; actions: EntityActions }) {
  const t = useTranslations("kg.explorer.entities.inspector");
  const format = useFormatter();
  const { edge } = detail;
  const end = (node: RelationDetail["src"]) => (
    <button
      type="button"
      onClick={() => actions.onSelectNode(node.id)}
      className="inline-flex min-w-0 cursor-pointer items-center gap-1.5 rounded-sm text-left font-semibold underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
    >
      <TypeDot kind={node.kind} />
      <span className="[overflow-wrap:anywhere]">{node.name}</span>
    </button>
  );
  return (
    <>
      <div className="flex flex-col gap-2">
        <Tag className="w-fit font-mono">{edge.rel}</Tag>
        <Heading testId="kg-relation-title">
          <span className="flex flex-col items-start gap-1">
            {end(detail.src)}
            <span className="text-xs font-normal text-muted-foreground">{t("relatesTo")}</span>
            {end(detail.dst)}
          </span>
        </Heading>
      </div>
      <Keywords text={edge.keywords} />
      <p className="text-[13px] leading-5 [overflow-wrap:anywhere]" data-testid="kg-relation-description">
        {edge.description ?? <span className="text-muted-foreground">{t("noDescription")}</span>}
      </p>
      <Figures
        rows={[
          [t("weight"), format.number(edge.weight, { maximumFractionDigits: 1 })],
          [t("statements"), format.number(edge.sources ?? 1)],
          [t("documents"), format.number(new Set(detail.citations.map((citation) => citation.document)).size)],
        ]}
      />
      <Provenance status={edge.status} model={edge.model} conf={edge.conf} label={edge.label} />
      <Citations project={project} citations={detail.citations} total={detail.citations.length} />
    </>
  );
}

export function RelationInspector({ project, id, actions }: { project: string; id: string; actions: EntityActions }) {
  const t = useTranslations("kg.explorer.entities.inspector");
  const state = useHubQuery(relationDetailQuery(browserApi, project, id), null);
  return (
    <Frame overline={t("relation")} onClose={actions.onClose} testId="kg-inspector-relation">
      {state.status === "error" && state.error.status === 404 ? (
        <p className="text-sm text-muted-foreground">{t("relationNotFound")}</p>
      ) : (
        <QueryView state={state} loading={<Loading />}>
          {(detail) => <RelationBody project={project} detail={detail} actions={actions} />}
        </QueryView>
      )}
    </Frame>
  );
}
