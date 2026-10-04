"use client";

import { Info, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback, useMemo, useState } from "react";

import { QueryView, useHubQuery } from "@/components/states/query-view";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useIsMobile } from "@/hooks/use-mobile";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { displayName, kindsOf, neighboursOf } from "@/lib/kg/graph";
import { kgNeighbourhoodQuery } from "@/lib/kg/queries";
import { nodeHref } from "@/lib/kg/routes";
import { type GraphNode, type Hops, MAX_NODES, type Neighbourhood } from "@/lib/kg/types";
import { cn } from "@/lib/utils";

import { KindShape } from "./badges";
import { GraphCanvas, type LayoutName, LAYOUTS } from "./graph-canvas";
import { NeighbourTable } from "./neighbour-table";

const SEGMENT =
  "inline-flex h-8 items-center rounded-md px-3 text-sm font-medium transition-colors hover:bg-muted hover:text-foreground";

function HopsToggle({ project, id, hops }: { project: string; id: string; hops: Hops }) {
  const t = useTranslations("kg.graph");
  return (
    <div role="group" aria-label={t("hops")} className="inline-flex rounded-lg border bg-card p-0.5" data-testid="kg-hops">
      {([1, 2] as const).map((value) => (
        <Link
          key={value}
          href={nodeHref(project, id, value)}
          scroll={false}
          aria-current={value === hops ? "true" : undefined}
          className={cn(SEGMENT, value === hops ? "bg-accent text-accent-foreground" : "text-muted-foreground")}
        >
          {t("hopsValue", { count: value })}
        </Link>
      ))}
    </div>
  );
}

function Legend({ nodes }: { nodes: GraphNode[] }) {
  const t = useTranslations("kg.graph");
  return (
    <div className="flex flex-col gap-1.5 text-xs text-muted-foreground">
      <p className="font-medium text-foreground">{t("legend")}</p>
      <ul className="flex flex-wrap gap-x-4 gap-y-1.5">
        {kindsOf(nodes).map((kind) => (
          <li key={kind} className="inline-flex items-center gap-1.5">
            <KindShape kind={kind} />
            {kind}
          </li>
        ))}
        <li className="inline-flex items-center gap-1.5">
          <span className="size-3 rounded-full border-[3px] border-primary" aria-hidden="true" />
          {t("legendFocus")}
        </li>
        <li className="inline-flex items-center gap-1.5">
          <span className="size-3 rounded-full border-2 border-dashed border-muted-foreground" aria-hidden="true" />
          {t("legendHub")}
        </li>
      </ul>
    </div>
  );
}

function View({ project, view }: { project: string; view: Neighbourhood }) {
  const t = useTranslations("kg.graph");
  const router = useRouter();
  const isMobile = useIsMobile();
  const [selected, setSelected] = useState(view.focus);
  const [layout, setLayout] = useState<LayoutName>("concentric");
  const byId = useMemo(() => new Map(view.nodes.map((node) => [node.id, node])), [view.nodes]);
  const focus = byId.get(view.focus) ?? view.nodes[0];
  const node = byId.get(selected) ?? focus;
  const rows = useMemo(() => neighboursOf(view, node.id), [view, node.id]);
  const open = useCallback((id: string) => router.push(nodeHref(project, id)), [router, project]);
  const select = useCallback((id: string) => setSelected(id), []);
  const name = displayName(focus);

  return (
    <div className="flex flex-col gap-4">
      {view.truncated ? (
        <Alert data-testid="kg-truncated">
          <TriangleAlert aria-hidden="true" />
          <AlertTitle>{t("truncatedTitle", { shown: view.nodes.length, limit: view.limit })}</AlertTitle>
          <AlertDescription>
            {t.rich("truncatedDescription", {
              leftOut: view.left_out,
              neighbours: view.neighbours,
              link: (chunks) => <a href="#kg-relations">{chunks}</a>,
            })}
          </AlertDescription>
        </Alert>
      ) : null}
      <div className="grid gap-6 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <div className="hidden min-w-0 flex-col gap-3 md:flex xl:sticky xl:top-20 xl:self-start">
          <div className="flex flex-wrap items-end justify-between gap-2">
            <p id="kg-graph-help" className="max-w-xl text-xs text-muted-foreground">
              {t("help")}
            </p>
            <div className="flex items-center gap-2">
              <label htmlFor="kg-layout" className="text-xs font-medium">
                {t("layout")}
              </label>
              <select
                id="kg-layout"
                value={layout}
                onChange={(event) => setLayout(event.target.value as LayoutName)}
                className="h-8 cursor-pointer rounded-lg border border-input bg-transparent px-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
              >
                {LAYOUTS.map((value) => (
                  <option key={value} value={value}>
                    {t(`layouts.${value}`)}
                  </option>
                ))}
              </select>
            </div>
          </div>
          {!isMobile ? (
            <GraphCanvas
              view={view}
              selected={node.id}
              layout={layout}
              label={t("canvasLabel", { name, nodes: view.nodes.length, edges: view.edges.length })}
              describedBy="kg-graph-help"
              onSelect={select}
              onOpen={open}
            />
          ) : null}
          <Legend nodes={view.nodes} />
        </div>
        <div className="flex min-w-0 flex-col gap-3">
          <p className="flex items-start gap-2 rounded-lg border border-dashed px-3 py-2 text-xs text-muted-foreground md:hidden">
            <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
            {t("smallScreen")}
          </p>
          <NeighbourTable project={project} node={node} focus={focus} rows={rows} onSelect={select} />
        </div>
      </div>
      <p className="sr-only" aria-live="polite" data-testid="kg-selection-announcement">
        {node.id === focus.id ? "" : t("selected", { name: displayName(node), kind: node.kind, count: rows.length })}
      </p>
    </div>
  );
}

export function NeighbourhoodSection({
  project,
  id,
  hops,
  initialError,
}: {
  project: string;
  id: string;
  hops: Hops;
  initialError: ApiErrorInfo | null;
}) {
  const t = useTranslations("kg.graph");
  const state = useHubQuery(kgNeighbourhoodQuery(browserApi, project, id, hops), initialError);
  return (
    <section aria-labelledby="kg-graph-title">
      <Card data-testid="kg-neighbourhood">
        <CardHeader className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex flex-col gap-1">
            <CardTitle>
              <h2 id="kg-graph-title">{t("title")}</h2>
            </CardTitle>
            <CardDescription>{t("description", { hops, limit: MAX_NODES })}</CardDescription>
          </div>
          <HopsToggle project={project} id={id} hops={hops} />
        </CardHeader>
        <CardContent>
          <QueryView state={state} loading={<Skeleton className="h-[28rem] w-full rounded-lg" />}>
            {(view) => <View key={`${view.focus}:${view.hops}`} project={project} view={view} />}
          </QueryView>
        </CardContent>
      </Card>
    </section>
  );
}
