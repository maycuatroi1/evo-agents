"use client";

import { ArrowUpCircle, Database, Plus, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import type { Agent } from "@/lib/agents";
import {
  joins,
  MAX_LABEL_CHARS,
  outdated,
  PINNED_KINDS,
  type SpecEdge,
  type SpecNode,
  type SystemProblem,
  usedGraphs,
  withNode,
  withoutNode,
} from "@/lib/systems";

import { KIND_ICONS } from "./designer-nodes";
import { ProblemList } from "./problem-list";
import type { DesignerSlots } from "./slots";
import type { Designer } from "./use-designer";

type Props = {
  project: string;
  node: SpecNode;
  problems: readonly SystemProblem[];
  designer: Designer;
  canChange: boolean;
  slots: DesignerSlots;
  onAddGraph: (forNode: string) => void;
};

/** A draft text that goes into the spec when the field loses the focus, so typing makes one undo step, not many. */
function LabelField({ node, disabled, onCommit }: { node: SpecNode; disabled: boolean; onCommit: (label: string) => void }) {
  const t = useTranslations("systems.designer");
  const [text, setText] = useState(node.label ?? "");
  return (
    <label className="flex flex-col gap-1.5 text-sm font-medium">
      {t("label")}
      <Input
        value={text}
        maxLength={MAX_LABEL_CHARS}
        placeholder={node.profile ? `@${node.profile}` : t(`kinds.${node.kind}`)}
        onChange={(event) => setText(event.target.value)}
        onBlur={() => onCommit(text.trim())}
        onKeyDown={(event) => (event.key === "Enter" ? onCommit(text.trim()) : undefined)}
        disabled={disabled}
        data-testid="system-node-label"
      />
    </label>
  );
}

/**
 * The side panel of a selected node: its name on the canvas, what the hub finds wrong with it, its pin and Upgrade,
 * the agent's config (saving it pins the new version), the graphs an agent uses (its uses_kg edges), a way to join it
 * to another node without dragging, its edges, and Remove.
 */
export function NodePanel({ project, node, problems, designer, canChange, slots, onAddGraph }: Props) {
  const t = useTranslations("systems.designer");
  const { spec, change, select, pin, latest } = designer;
  const Icon = KIND_ICONS[node.kind];
  const agent: Agent | undefined = designer.agents.find((one) => one.handle === node.profile && one.deleted_at === null);
  const newer = outdated(node, latest);
  const graphs = spec.nodes.filter((other) => other.kind === "kg");
  const used = new Set(usedGraphs(spec, node.id));
  const targets = spec.nodes.filter((other) => joins(spec, node.id, other.id).length > 0);
  const touching = spec.edges.map((edge, index) => ({ edge, index })).filter(({ edge }) => edge.from === node.id || edge.to === node.id);
  const nameOf = (id: string) => {
    const other = spec.nodes.find((one) => one.id === id);
    return other?.label || (other?.profile ? `@${other.profile}` : other?.kind === "kg" ? (other.graph ?? id) : id);
  };

  const toggleGraph = (graph: string) =>
    change((current) => ({
      ...current,
      edges: used.has(graph)
        ? current.edges.filter((edge) => !(edge.from === node.id && edge.to === graph && edge.kind === "uses_kg"))
        : [...current.edges, { from: node.id, to: graph, kind: "uses_kg", description: "" } satisfies SpecEdge],
    }));

  const upgrade = async (version: number) => {
    const saved = await pin(node.id, version);
    notify({ tone: "success", text: t("pinnedTo", { handle: node.profile ?? "", version }), description: saved ? t("pinSaved") : t("pinDraft") });
  };

  return (
    <div className="flex flex-col gap-5" data-testid="system-node-panel" data-node={node.id}>
      <div className="flex items-center gap-2">
        <Icon className="size-5 text-muted-foreground" aria-hidden="true" />
        <h2 className="min-w-0 flex-1 truncate text-base font-semibold">{nameOf(node.id)}</h2>
        <span className="font-mono text-xs text-muted-foreground">{t(`kinds.${node.kind}`)}</span>
      </div>
      <ProblemList problems={problems} />
      <LabelField key={node.id} node={node} disabled={!canChange} onCommit={(label) => (label === (node.label ?? "") ? undefined : change(withNode(spec, node.id, { label: label || undefined })))} />

      {PINNED_KINDS.includes(node.kind) ? (
        <section className="flex flex-col gap-2 rounded-md border bg-surface-sunken p-3" aria-label={t("pin")} data-testid="system-node-pin">
          <p className="text-sm">
            {node.version ? t("pinned", { handle: node.profile ?? "", version: node.version }) : t("pinLatest", { handle: node.profile ?? "" })}
          </p>
          {newer !== null ? (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-attention">{t("newer", { version: newer })}</span>
              {canChange ? (
                <Button type="button" size="sm" variant="outline" onClick={() => void upgrade(newer)} data-testid="system-panel-upgrade">
                  <ArrowUpCircle aria-hidden="true" />
                  {t("upgradeTo", { version: newer })}
                </Button>
              ) : null}
            </div>
          ) : null}
        </section>
      ) : null}

      {node.kind === "agent" ? (
        <section className="flex flex-col gap-2" aria-labelledby="system-node-graphs">
          <h3 id="system-node-graphs" className="text-sm font-medium">
            {t("graphs")}
          </h3>
          {graphs.length ? (
            <ul className="flex flex-col gap-1">
              {graphs.map((graph) => (
                <li key={graph.id}>
                  <label className="flex items-center gap-2 text-sm">
                    <input type="checkbox" className="size-4 accent-[var(--brand)]" checked={used.has(graph.id)} onChange={() => toggleGraph(graph.id)} disabled={!canChange} data-testid="system-node-graph" data-graph={graph.id} />
                    <Database className="size-3.5 text-muted-foreground" aria-hidden="true" />
                    <span className="truncate">{nameOf(graph.id)}</span>
                  </label>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-muted-foreground">{t("noGraphs")}</p>
          )}
          {canChange ? (
            <Button type="button" variant="ghost" size="sm" className="w-fit" onClick={() => onAddGraph(node.id)} data-testid="system-node-add-graph">
              <Plus aria-hidden="true" />
              {t("addGraph")}
            </Button>
          ) : null}
        </section>
      ) : null}

      {canChange && targets.length ? (
        <label className="flex flex-col gap-1.5 text-sm font-medium">
          {t("connect")}
          <NativeSelect
            value=""
            className="w-full"
            onChange={(event) => {
              const made = joins(spec, node.id, event.target.value).find((join) => join.from === node.id) ?? joins(spec, node.id, event.target.value)[0];
              if (!made) return;
              change((current) => ({ ...current, edges: [...current.edges, { ...made, description: "" }] }));
              select({ edge: spec.edges.length });
            }}
            data-testid="system-node-connect"
          >
            <NativeSelectOption value="">{t("connectPick")}</NativeSelectOption>
            {targets.map((other) => (
              <NativeSelectOption key={other.id} value={other.id}>
                {`${nameOf(other.id)} (${t(`kinds.${other.kind}`)})`}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </label>
      ) : null}

      {touching.length ? (
        <section className="flex flex-col gap-1" aria-labelledby="system-node-edges">
          <h3 id="system-node-edges" className="text-sm font-medium">
            {t("edges")}
          </h3>
          {touching.map(({ edge, index }) => (
            <button key={index} type="button" className="truncate rounded-sm px-1.5 py-1 text-left text-sm hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none" onClick={() => select({ edge: index })}>
              {t("edgeLine", { from: nameOf(edge.from), kind: t(`edgeKinds.${edge.kind}`), to: nameOf(edge.to) })}
            </button>
          ))}
        </section>
      ) : null}

      {PINNED_KINDS.includes(node.kind) && agent ? (
        <section className="flex flex-col gap-3 border-t pt-4" aria-labelledby="system-node-config">
          <div className="flex flex-col gap-1">
            <h3 id="system-node-config" className="text-sm font-medium">
              {t("config")}
            </h3>
            <p className="text-xs text-pretty text-muted-foreground">
              {node.version && node.version !== agent.version ? t("configLatest", { version: agent.version, pinned: node.version }) : t("configHint")}
            </p>
          </div>
          <slots.AgentPanel
            key={`${agent.handle}@${agent.version}`}
            project={project}
            agent={agent}
            canChange={canChange}
            onSaved={(saved) => void upgrade(saved.version)}
          />
        </section>
      ) : null}

      {canChange && node.kind !== "entry" ? (
        <Button
          type="button"
          variant="quiet-danger"
          className="w-fit"
          onClick={() => {
            change(withoutNode(spec, node.id));
            select(null);
          }}
          data-testid="system-node-remove"
        >
          <Trash2 aria-hidden="true" />
          {t("remove")}
        </Button>
      ) : null}
    </div>
  );
}
