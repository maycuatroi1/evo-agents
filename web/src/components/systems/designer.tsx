"use client";

import { ReactFlowProvider, useReactFlow } from "@xyflow/react";
import { X } from "lucide-react";
import { useTranslations } from "next-intl";
import { type KeyboardEvent, useCallback, useEffect, useMemo, useState } from "react";

import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import type { Agent } from "@/lib/agents";
import { freeId, type Join, type NodeKind, placeFor, placeProblems, type Spec, type SpecNode, withoutNode } from "@/lib/systems";
import { laidOut } from "@/lib/systems-layout";

import { type Adding, AddNodeDialog } from "./add-node-dialog";
import { ConflictBanner } from "./conflict-banner";
import { DesignerCanvas } from "./designer-canvas";
import { DESIGNER_FRAME, type DesignerProps } from "./designer-frame";
import { DesignerToolbar } from "./designer-toolbar";
import { EdgePanel } from "./edge-panel";
import { NodePanel } from "./node-panel";
import { ProblemList } from "./problem-list";
import { TestSheet } from "./test-sheet";
import { useDesigner } from "./use-designer";

const EDITABLE = "input, textarea, select, [contenteditable=true]";

/** `spec` with `node` added and, for the first agent, the entry's edge to it, so the system has an entry agent. */
function withAdded(spec: Spec, node: SpecNode, edgeFrom: string | null): Spec {
  const entry = spec.nodes.find((one) => one.kind === "entry");
  const edges = [...spec.edges];
  if (node.kind === "agent" && entry && !spec.edges.some((edge) => edge.from === entry.id && edge.kind === "interacts")) {
    edges.push({ from: entry.id, to: node.id, kind: "interacts", description: "" });
  }
  if (edgeFrom) edges.push({ from: edgeFrom, to: node.id, kind: "uses_kg", description: "" });
  return { ...spec, nodes: [...spec.nodes, node], edges };
}

function Designer({ project, system, slots }: DesignerProps) {
  const t = useTranslations("systems.designer");
  const flow = useReactFlow();
  const designer = useDesigner(project, system);
  const { spec, selection, select, change, dirty } = designer;
  const canChange = system.can_change;
  const [adding, setAdding] = useState<Adding>(null);
  const [testing, setTesting] = useState(false);
  const placed = useMemo(() => placeProblems(designer.problems), [designer.problems]);
  const errors = designer.problems.filter((problem) => problem.level === "error").length;

  // Leaving the page with unsaved changes asks first.
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const node = selection && "node" in selection ? spec.nodes.find((one) => one.id === selection.node) : undefined;
  const edgeIndex = selection && "edge" in selection && selection.edge < spec.edges.length ? selection.edge : null;
  const edge = edgeIndex === null ? undefined : spec.edges[edgeIndex];

  const add = (made: SpecNode, edgeFrom: string | null = null) => {
    change(withAdded(spec, made, edgeFrom));
    select({ node: made.id });
    setAdding(null);
  };
  const addKind = (kind: NodeKind) => {
    if (kind === "agent" || kind === "orchestrator") setAdding({ kind });
    else if (kind === "kg") setAdding({ kind: "kg", forNode: null });
    else add({ id: freeId(spec, kind), kind, position: placeFor(spec) });
  };
  const addProfile = (agent: Agent) => {
    if (!adding || adding.kind === "kg") return;
    add({ id: freeId(spec, agent.handle), kind: adding.kind, profile: agent.handle, version: agent.version, position: placeFor(spec) });
  };
  const addGraph = (graph: string) => {
    const forNode = adding?.kind === "kg" ? adding.forNode : null;
    add({ id: freeId(spec, graph === "default" ? "kg" : graph), kind: "kg", graph, position: placeFor(spec) }, forNode);
  };
  const onJoin = useCallback(
    (join: Join) => {
      change((current) => ({ ...current, edges: [...current.edges, { ...join, description: "" }] }));
      select({ edge: spec.edges.length });
    },
    [change, select, spec.edges.length],
  );
  const { pin } = designer;
  const onUpgrade = useCallback(
    (id: string, version: number) => {
      const profile = spec.nodes.find((one) => one.id === id)?.profile ?? "";
      void pin(id, version).then((saved) =>
        notify({ tone: "success", text: t("pinnedTo", { handle: profile, version }), description: saved ? t("pinSaved") : t("pinDraft") }),
      );
    },
    [pin, spec.nodes, t],
  );
  const layout = () => {
    change(laidOut(spec));
    requestAnimationFrame(() => void flow.fitView({ padding: 0.2, maxZoom: 1, duration: 200 }));
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest(EDITABLE) || !canChange) return;
    const mod = event.metaKey || event.ctrlKey;
    const key = event.key.toLowerCase();
    if (mod && key === "z") {
      event.preventDefault();
      if (event.shiftKey) designer.redo();
      else designer.undo();
    } else if (mod && key === "y") {
      event.preventDefault();
      designer.redo();
    } else if (mod && key === "s") {
      event.preventDefault();
      if (dirty && errors === 0 && !designer.conflict) void designer.saveDraft();
    } else if ((event.key === "Delete" || event.key === "Backspace") && (node || edge)) {
      event.preventDefault();
      if (node && node.kind !== "entry") change(withoutNode(spec, node.id));
      else if (edgeIndex !== null) change({ ...spec, edges: spec.edges.filter((_, at) => at !== edgeIndex) });
      select(null);
    }
  };

  const testBlocked = !system.entry_agent ? t("testNoEntry") : null;

  return (
    <div className="flex flex-col gap-3">
      <ConflictBanner designer={designer} />
      {/* The frame takes the keys of the designer: undo, redo, save and delete. */}
      <div className={DESIGNER_FRAME} onKeyDown={onKeyDown} data-testid="system-designer" data-revision={designer.revision}>
        <DesignerToolbar designer={designer} canChange={canChange} errors={errors} onAdd={addKind} onLayout={layout} onTest={() => setTesting(true)} testBlocked={testBlocked} />
        {placed.system.length ? <ProblemList problems={placed.system} className="border-b px-3 py-2" /> : null}
        <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
          <div className="system-graph relative min-h-[320px] min-w-0 flex-1 bg-background" data-testid="system-canvas">
            <DesignerCanvas designer={designer} placed={placed} canChange={canChange} onJoin={onJoin} onUpgrade={onUpgrade} />
            {spec.nodes.length <= 1 && canChange ? (
              <p className="pointer-events-none absolute inset-x-0 bottom-4 mx-auto w-fit max-w-[90%] rounded-md border bg-card px-3 py-2 text-center text-[13px] text-pretty text-muted-foreground shadow-raised">
                {t("emptyHint")}
              </p>
            ) : null}
          </div>
          {node || edge ? (
            <aside className="relative flex min-h-0 flex-col overflow-y-auto border-t bg-card p-4 max-lg:max-h-[55%] lg:w-[24rem] lg:border-t-0 lg:border-l" aria-label={t("panel")} data-testid="system-panel">
              <Button type="button" variant="ghost" size="icon-sm" className="absolute top-3 right-3" aria-label={t("closePanel")} onClick={() => select(null)}>
                <X aria-hidden="true" />
              </Button>
              {node ? (
                <NodePanel key={node.id} project={project} node={node} problems={placed.nodes.get(node.id) ?? []} designer={designer} canChange={canChange} slots={slots} onAddGraph={(forNode) => setAdding({ kind: "kg", forNode })} />
              ) : edge && edgeIndex !== null ? (
                <EdgePanel key={`${edgeIndex}-${edge.from}-${edge.to}-${edge.description}`} edge={edge} index={edgeIndex} problems={placed.edges.get(edgeIndex) ?? []} designer={designer} canChange={canChange} />
              ) : null}
            </aside>
          ) : null}
        </div>
      </div>
      <AddNodeDialog project={project} adding={adding} spec={spec} agents={designer.agents} slots={slots} onClose={() => setAdding(null)} onProfile={addProfile} onGraph={addGraph} />
      {system.entry_agent ? (
        <TestSheet project={project} slug={system.slug} entry={system.entry_agent} revision={system.revision} dirty={dirty} open={testing} onOpenChange={setTesting} slots={slots} />
      ) : null}
    </div>
  );
}

/** The designer of an agent system (web/DESIGN.md, Agent systems); loaded on demand by designer-lazy.tsx. */
export default function SystemDesigner(props: DesignerProps) {
  return (
    <ReactFlowProvider>
      <Designer {...props} />
    </ReactFlowProvider>
  );
}

