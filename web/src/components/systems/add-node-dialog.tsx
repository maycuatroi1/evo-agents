"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Database, ExternalLink, Plus } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { InlineError, useWriteFailure } from "@/components/feedback/notice";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import type { Agent } from "@/lib/agents";
import { browserApi } from "@/lib/api/browser";
import { createGraph, DEFAULT_GRAPH, toggled, validGraphName } from "@/lib/kg/graphs";
import { kgGraphsQuery, kgKeys } from "@/lib/kg/queries";
import { uploadHref } from "@/lib/kg/routes";
import type { Spec } from "@/lib/systems";

import type { DesignerSlots } from "./slots";

export type Adding = { kind: "agent" | "orchestrator" } | { kind: "kg"; forNode: string | null } | null;

type Props = {
  project: string;
  adding: Adding;
  spec: Spec;
  agents: readonly Agent[];
  slots: DesignerSlots;
  onClose: () => void;
  /** An agent or orchestrator node for this profile at this version. */
  onProfile: (agent: Agent) => void;
  /** A kg node for this named graph. */
  onGraph: (graph: string) => void;
};

function PickAgent({ project, kind, spec, agents, slots, onPick }: { project: string; kind: "agent" | "orchestrator"; spec: Spec; agents: readonly Agent[]; slots: DesignerSlots; onPick: (agent: Agent) => void }) {
  const t = useTranslations("systems.add");
  const taken = new Set(spec.nodes.map((node) => node.profile).filter(Boolean));
  const free = agents.filter((agent) => agent.deleted_at === null && !taken.has(agent.handle));
  const [handle, setHandle] = useState(free[0]?.handle ?? "");
  const [creating, setCreating] = useState(false);
  const picked = free.find((agent) => agent.handle === handle);
  return (
    <>
      <div className="flex flex-col gap-3">
        <label className="flex flex-col gap-1.5 text-sm font-medium">
          {t("existing")}
          {free.length ? (
            <NativeSelect value={handle} onChange={(event) => setHandle(event.target.value)} className="w-full" data-testid="system-add-profile">
              {free.map((agent) => (
                <NativeSelectOption key={agent.handle} value={agent.handle}>
                  {`${agent.name} (@${agent.handle}, v${agent.version})`}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          ) : (
            <span className="text-sm font-normal text-muted-foreground">{t("noFreeAgent")}</span>
          )}
        </label>
        <p className="text-xs text-pretty text-muted-foreground">{t(kind === "agent" ? "agentHint" : "orchestratorHint")}</p>
      </div>
      <DialogFooter className="sm:justify-between">
        <Button type="button" variant="ghost" onClick={() => setCreating(true)} data-testid="system-add-new-agent">
          <Plus aria-hidden="true" />
          {t("newAgent")}
        </Button>
        <Button type="button" disabled={!picked} onClick={() => picked && onPick(picked)} data-testid="system-add-confirm">
          <Bot aria-hidden="true" />
          {t("addProfile")}
        </Button>
      </DialogFooter>
      <slots.NewAgent project={project} open={creating} onOpenChange={setCreating} onSaved={onPick} />
    </>
  );
}

function PickGraph({ project, spec, onPick }: { project: string; spec: Spec; onPick: (graph: string) => void }) {
  const t = useTranslations("systems.add");
  const failure = useWriteFailure();
  const queryClient = useQueryClient();
  const graphs = useQuery(kgGraphsQuery(browserApi, project));
  const taken = new Set(spec.nodes.filter((node) => node.kind === "kg").map((node) => node.graph ?? DEFAULT_GRAPH));
  const free = (graphs.data?.graphs ?? []).filter((graph) => !taken.has(graph.name));
  const [name, setName] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  const [sources, setSources] = useState<string[]>([]);
  const choice = picked ?? free[0]?.name ?? "";
  const create = useMutation({
    mutationFn: () => createGraph(browserApi(), project, { name: name.trim(), sources, description: "" }),
    onSuccess: (graph) => {
      void queryClient.invalidateQueries({ queryKey: kgKeys.graphs(project) });
      onPick(graph.name);
    },
  });
  const nameBad = name.trim() !== "" && (!validGraphName(name.trim()) || taken.has(name.trim()));
  const error = create.isError ? failure(create.error) : null;
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-2">
        <label className="flex flex-col gap-1.5 text-sm font-medium">
          {t("existingGraph")}
          {free.length ? (
            <NativeSelect value={choice} onChange={(event) => setPicked(event.target.value)} className="w-full" data-testid="system-add-graph">
              {free.map((graph) => (
                <NativeSelectOption key={graph.name} value={graph.name}>
                  {graph.default ? t("defaultGraph") : graph.name}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          ) : (
            <span className="text-sm font-normal text-muted-foreground">{graphs.isPending ? t("loading") : t("noFreeGraph")}</span>
          )}
        </label>
        <Button type="button" className="w-fit" disabled={!choice} onClick={() => onPick(choice)} data-testid="system-add-graph-confirm">
          <Database aria-hidden="true" />
          {t("addGraph")}
        </Button>
      </div>
      {graphs.data?.can_change ? (
        <fieldset className="flex flex-col gap-3 border-t pt-4" data-testid="system-new-graph">
          <legend className="text-sm font-medium">{t("newGraph")}</legend>
          <label className="flex flex-col gap-1.5 text-sm">
            {t("graphName")}
            <Input value={name} onChange={(event) => setName(event.target.value.toLowerCase())} aria-invalid={nameBad || undefined} spellCheck={false} className="font-mono" data-testid="system-new-graph-name" />
            {nameBad ? <span className="text-xs text-danger">{t("graphNameBad")}</span> : <span className="text-xs text-muted-foreground">{t("graphNameHint")}</span>}
          </label>
          <div className="flex flex-col gap-1.5">
            <span className="text-sm">{t("sources")}</span>
            {graphs.data.sources.length ? (
              <ul className="flex max-h-40 flex-col gap-1 overflow-y-auto">
                {graphs.data.sources.map((source) => (
                  <li key={source}>
                    <label className="flex items-center gap-2 font-mono text-[13px]">
                      <input type="checkbox" className="size-4 accent-[var(--brand)]" checked={sources.includes(source)} onChange={() => setSources(toggled(sources, source))} />
                      {source}
                    </label>
                  </li>
                ))}
              </ul>
            ) : (
              <span className="text-xs text-muted-foreground">{t("noSources")}</span>
            )}
            <Link href={uploadHref(project)} target="_blank" className="inline-flex w-fit items-center gap-1 text-sm text-brand hover:underline" data-testid="system-upload-link">
              {t("upload")}
              <ExternalLink className="size-3.5" aria-hidden="true" />
            </Link>
          </div>
          {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
          <Button type="button" variant="outline" className="w-fit" busy={create.isPending} disabled={!name.trim() || nameBad || !sources.length} onClick={() => create.mutate()} data-testid="system-new-graph-create">
            <Plus aria-hidden="true" />
            {t("createGraph")}
          </Button>
        </fieldset>
      ) : null}
    </div>
  );
}

/**
 * Add an agent, an orchestrator or a knowledge graph to the system: one the project has (a profile not on the canvas
 * yet, a named graph not on it yet), or a new one: New agent opens the Agents form, New graph names a graph of the
 * sources the project's last build read, with a link to the upload that adds a source.
 */
export function AddNodeDialog({ project, adding, spec, agents, slots, onClose, onProfile, onGraph }: Props) {
  const t = useTranslations("systems.add");
  return (
    <Dialog open={adding !== null} onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent className="sm:max-w-lg" data-testid="system-add-dialog" data-kind={adding?.kind ?? ""}>
        {adding ? (
          <>
            <DialogHeader>
              <DialogTitle>{t(`title.${adding.kind}`)}</DialogTitle>
              <DialogDescription>{t(`description.${adding.kind}`)}</DialogDescription>
            </DialogHeader>
            {adding.kind === "kg" ? (
              <PickGraph project={project} spec={spec} onPick={onGraph} />
            ) : (
              <PickAgent project={project} kind={adding.kind} spec={spec} agents={agents} slots={slots} onPick={onProfile} />
            )}
          </>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
