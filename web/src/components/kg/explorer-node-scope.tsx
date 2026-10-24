"use client";

import { useQueries } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { useCallback, useMemo, useState } from "react";

import { ApiErrorState } from "@/components/states/states";
import { browserApi } from "@/lib/api/browser";
import { OVERVIEW, type Selection } from "@/lib/kg/explorer";
import {
  collapse,
  EXPAND_GROUP,
  type Expansion,
  expand,
  kindsInView,
  mergeExpansions,
  openGroup,
  parseMoreId,
  relationGroupKey,
  WORKING_CAP,
  workingElements,
} from "@/lib/kg/explorer-working";
import { displayName } from "@/lib/kg/graph";
import { kgNeighbourhoodQuery } from "@/lib/kg/queries";
import type { Relation } from "@/lib/kg/types";

import { ExplorerCanvas } from "./explorer-canvas";
import { HELP_ID, Legend, Trail, Workspace } from "./explorer-frame";
import { type NodeActions, NodeInspector } from "./explorer-inspector";
import { ExpandedList } from "./explorer-rail";
import { CanvasState, NotFound, type ScopeProps } from "./explorer-scopes";

/**
 * The explorer at a node: the node and its direct neighbours (the most linked few of each group, "+N" for the rest),
 * and the neighbours of each node the reader expands, merged into one working set (lib/kg/explorer-working.ts). Each
 * expansion is a neighbourhood query of its own, cached by the node and the groups opened, so collapsing and expanding
 * again reads nothing twice. A node can be hidden; the node the view started from cannot.
 */
export function NodeScope({ seed, project, hiddenKinds, layout, onLayout, search, shared, navigate }: ScopeProps & { seed: string }) {
  const t = useTranslations("kg.explorer");
  const format = useFormatter();
  const [expansions, setExpansions] = useState<Expansion[]>([{ id: seed, open: [] }]);
  const [hiddenNodes, setHiddenNodes] = useState<ReadonlySet<string>>(new Set());
  const [selection, setSelection] = useState<Selection | undefined>(undefined);
  const results = useQueries({
    queries: expansions.map((e) => kgNeighbourhoodQuery(browserApi, project, e.id, 1, { group: EXPAND_GROUP, open: e.open })),
    combine: (all) => ({
      views: all.map((result) => result.data),
      fetching: all.some((result) => result.isFetching),
      seedError: all[0]?.error ?? null,
      retry: () => void all[0]?.refetch(),
    }),
  });
  const working = useMemo(() => mergeExpansions(seed, expansions, results.views, hiddenNodes), [seed, expansions, results.views, hiddenNodes]);
  const elements = useMemo(() => workingElements(working, hiddenKinds), [working, hiddenKinds]);
  const canExpand = working.nodes.size < WORKING_CAP;
  // The node's own id once its neighbourhood is read: the URL may name it by an alias. Its expansion keeps the alias.
  const root = working.seed;
  const key = useCallback((id: string) => (id === root ? seed : id), [root, seed]);
  const effective: Selection = selection === undefined ? { type: "node", id: root } : selection;
  const expanded = (id: string) => expansions.some((e) => e.id === key(id));
  const name = (id: string) => {
    const node = working.nodes.get(id);
    return node ? displayName(node) : id;
  };

  const select = useCallback((id: string | null) => setSelection(id ? { type: "node", id } : null), []);
  const hide = useCallback(
    (id: string) => {
      if (id === root || id === seed) return;
      setHiddenNodes((now) => new Set([...now, id]));
      setExpansions((now) => collapse(now, id, seed));
      setSelection(null);
    },
    [root, seed],
  );
  const activate = useCallback(
    (id: string) => {
      const more = parseMoreId(id);
      if (more) {
        setExpansions((now) => openGroup(now, key(more.expansion), more.group));
        return;
      }
      setSelection({ type: "node", id });
      setExpansions((now) => {
        if (now.some((e) => e.id === key(id))) return key(id) === seed ? now : collapse(now, id, seed);
        return canExpand ? expand(now, id) : now;
      });
    },
    [canExpand, seed, key],
  );
  const reveal = (relation: Relation, direction: "out" | "in") => {
    setHiddenNodes((now) => (now.has(relation.node) ? new Set([...now].filter((id) => id !== relation.node)) : now));
    if (!working.nodes.has(relation.node) && effective?.type === "node") {
      const from = effective.id;
      setExpansions((now) => openGroup(now, key(from), relationGroupKey(direction, relation.rel, relation.kind)));
    }
    setSelection({ type: "node", id: relation.node });
  };
  const actions: NodeActions = {
    working: effective?.type === "node" ? { expanded: expanded(effective.id), seed: key(effective.id) === seed, canExpand } : undefined,
    onExpand: (id) => setExpansions((now) => expand(now, key(id))),
    onCollapse: (id) => setExpansions((now) => collapse(now, key(id), seed)),
    onHide: hide,
    onExplore: (id) => navigate({ kind: "node", id }),
    onRelation: reveal,
    onClose: () => setSelection(null),
  };
  const missing = results.seedError?.info.status === 404;
  const canvas = missing ? (
    <CanvasState>
      <NotFound text={t("nodeNotFound")} navigate={navigate} />
    </CanvasState>
  ) : results.seedError && !results.views[0] ? (
    <CanvasState>
      <ApiErrorState error={results.seedError.info} onRetry={results.retry} />
    </CanvasState>
  ) : !results.views[0] ? null : (
    <ExplorerCanvas
      scope={{ kind: "node", id: seed }}
      elements={elements}
      selected={effective?.id ?? null}
      highlight={null}
      layout={layout}
      root={root}
      label={t("canvasNode", { name: name(root), nodes: working.nodes.size, edges: working.edges.size })}
      describedBy={HELP_ID}
      onSelect={select}
      onActivate={activate}
      onBack={() => navigate(OVERVIEW)}
      onHide={hide}
    />
  );
  return (
    <Workspace
      scope="node"
      search={search}
      rail={
        <>
          {shared(results.views[0] ? new Set([...working.nodes.values()].map((node) => node.kind)) : null)}
          <ExpandedList
            rows={expansions.map((e) => {
              const id = e.id === seed ? root : e.id;
              return { id, name: name(id), kind: working.nodes.get(id)?.kind ?? "", seed: e.id === seed };
            })}
            hidden={hiddenNodes.size}
            selected={effective?.id ?? null}
            onSelect={(id) => setSelection({ type: "node", id })}
            onCollapse={actions.onCollapse}
            onShowHidden={() => setHiddenNodes(new Set())}
          />
        </>
      }
      trail={<Trail crumbs={[{ label: t("overview"), onClick: () => navigate(OVERVIEW) }, { label: name(root) }]} />}
      layout={layout}
      onLayout={onLayout}
      canvas={canvas}
      status={results.fetching ? t("reading") : canExpand ? null : t("capReached", { count: format.number(working.nodes.size) })}
      legend={
        <Legend
          scope="node"
          kinds={kindsInView(working.nodes.values()).map((k) => k.kind)}
          counts={t("countsNode", { nodes: working.nodes.size, edges: working.edges.size })}
        />
      }
      inspector={missing || effective?.type !== "node" ? null : <NodeInspector key={effective.id} project={project} id={effective.id} actions={actions} />}
      announcement={effective?.type === "node" && working.nodes.has(effective.id) ? t("announceNode", { name: name(effective.id), kind: working.nodes.get(effective.id)!.kind }) : ""}
    />
  );
}
