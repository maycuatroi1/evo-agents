"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Settings2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { useHubQuery } from "@/components/states/query-view";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import {
  createGraph,
  DEFAULT_GRAPH,
  deleteGraph,
  MAX_GRAPH_DESCRIPTION,
  toggled,
  updateGraph,
  validGraphName,
} from "@/lib/kg/graphs";
import { kgGraphsQuery, kgKeys } from "@/lib/kg/queries";
import type { NamedGraph } from "@/lib/kg/types";

/**
 * The KG picker of the explorer: `default` (every source) or a named graph of the project, which narrows the rail's
 * search to its sources (`?graph=`). A writer opens the manager beside it to create, rename, give or strip sources
 * and delete named graphs (docs/kg.md, Named graphs).
 */
export function GraphPicker({ project, graph, onPick }: { project: string; graph: string; onPick: (name: string) => void }) {
  const t = useTranslations("kg.graphs");
  const id = useId();
  const [managing, setManaging] = useState(false);
  const graphs = useHubQuery(kgGraphsQuery(browserApi, project), null);
  const data = graphs.status === "success" ? graphs.data : null;
  const named = data?.graphs.filter((g) => !g.default) ?? [];
  const known = graph === "" || named.some((g) => g.name === graph);
  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="kg-graph-picker">
      <Label htmlFor={id} className="text-sm text-muted-foreground">
        {t("label")}
      </Label>
      <NativeSelect
        id={id}
        size="sm"
        value={graph || DEFAULT_GRAPH}
        onChange={(event) => onPick(event.target.value === DEFAULT_GRAPH ? "" : event.target.value)}
        data-testid="kg-graph-select"
      >
        <NativeSelectOption value={DEFAULT_GRAPH}>{t("default")}</NativeSelectOption>
        {named.map((g) => (
          <NativeSelectOption key={g.name} value={g.name}>
            {g.name}
          </NativeSelectOption>
        ))}
        {known ? null : <NativeSelectOption value={graph}>{graph}</NativeSelectOption>}
      </NativeSelect>
      {data?.can_change ? (
        <Button type="button" size="sm" variant="ghost" onClick={() => setManaging(true)} data-testid="kg-graph-manage">
          <Settings2 aria-hidden="true" />
          {t("manage")}
        </Button>
      ) : null}
      {data ? (
        <GraphManager
          open={managing}
          onOpenChange={setManaging}
          project={project}
          graphs={named}
          sources={data.sources}
          onRenamed={(from, to) => (graph === from ? onPick(to) : undefined)}
          onDeleted={(name) => (graph === name ? onPick("") : undefined)}
        />
      ) : null}
    </div>
  );
}

/** A write of the manager: its failure in place, a toast and a fresh list when done. */
function useGraphWrite<T>(project: string, write: (input: T) => Promise<unknown>, done: (input: T) => string, after?: (input: T) => void) {
  const t = useTranslations("kg.graphs");
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const [error, setError] = useState<WriteFailure | null>(null);
  const save = useMutation({
    mutationFn: write,
    onSuccess: async (_, input) => {
      setError(null);
      notify({ tone: "success", text: done(input) });
      after?.(input);
      await queryClient.invalidateQueries({ queryKey: kgKeys.graphs(project) });
      await queryClient.invalidateQueries({ queryKey: [...kgKeys.all(project), "search"] });
    },
    onError: (cause) => setError(failure(cause, { 403: t("forbidden"), 404: t("gone"), 409: t("taken"), 422: t("invalid") })),
  });
  return { save, error };
}

function GraphManager({
  open,
  onOpenChange,
  project,
  graphs,
  sources,
  onRenamed,
  onDeleted,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  project: string;
  graphs: NamedGraph[];
  sources: string[];
  onRenamed: (from: string, to: string) => void;
  onDeleted: (name: string) => void;
}) {
  const t = useTranslations("kg.graphs");
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg" aria-describedby={undefined} data-testid="kg-graph-manager">
        <DialogHeader>
          <DialogTitle>{t("title")}</DialogTitle>
        </DialogHeader>
        <ul className="flex flex-col">
          {graphs.map((graph) => (
            <GraphRow
              key={graph.name}
              project={project}
              graph={graph}
              sources={sources}
              onRenamed={onRenamed}
              onDeleted={onDeleted}
            />
          ))}
        </ul>
        <CreateForm project={project} sources={sources} />
      </DialogContent>
    </Dialog>
  );
}

function SourceBoxes({ all, held, onToggle, name }: { all: string[]; held: string[]; onToggle: (source: string) => void; name: string }) {
  const t = useTranslations("kg.graphs");
  const shown = [...new Set([...all, ...held])].sort();
  if (shown.length === 0) return <p className="text-sm text-muted-foreground">{t("noSources")}</p>;
  return (
    <fieldset className="flex flex-col gap-1">
      <legend className="pb-1 text-sm font-medium">{t("sources")}</legend>
      {shown.map((source) => (
        <label key={source} className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={held.includes(source)}
            onChange={() => onToggle(source)}
            className="size-4 accent-primary"
            data-testid={`kg-graph-source-${name}`}
            value={source}
          />
          <code className="font-mono text-xs break-all">{source}</code>
          {all.includes(source) ? null : <span className="text-xs text-muted-foreground">{t("notBuilt")}</span>}
        </label>
      ))}
    </fieldset>
  );
}

function GraphRow({
  project,
  graph,
  sources,
  onRenamed,
  onDeleted,
}: {
  project: string;
  graph: NamedGraph;
  sources: string[];
  onRenamed: (from: string, to: string) => void;
  onDeleted: (name: string) => void;
}) {
  const t = useTranslations("kg.graphs");
  const nameId = useId();
  const [name, setName] = useState(graph.name);
  const [held, setHeld] = useState<string[]>(graph.sources);
  const update = useGraphWrite(
    project,
    (body: { rename?: string; sources: string[] }) => updateGraph(browserApi(), project, graph.name, body),
    () => t("saved", { name: name.trim() }),
    (body) => (body.rename ? onRenamed(graph.name, body.rename) : undefined),
  );
  const remove = useGraphWrite(
    project,
    () => deleteGraph(browserApi(), project, graph.name),
    () => t("deleted", { name: graph.name }),
    () => onDeleted(graph.name),
  );
  const next = name.trim();
  const renamed = next !== graph.name;
  const changed = renamed || held.join("\n") !== graph.sources.join("\n");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!changed || (renamed && !validGraphName(next))) return;
    update.save.mutate({ ...(renamed ? { rename: next } : {}), sources: held });
  };
  const error = update.error ?? remove.error;
  return (
    <li className="border-t py-3 first:border-t-0" data-testid="kg-graph" data-name={graph.name}>
      <form onSubmit={submit} className="flex flex-col gap-2">
        <Label htmlFor={nameId}>{t("name")}</Label>
        <Input id={nameId} value={name} maxLength={63} onChange={(event) => setName(event.target.value)} aria-invalid={renamed && !validGraphName(next)} />
        <SourceBoxes all={sources} held={held} onToggle={(source) => setHeld((now) => toggled(now, source))} name={graph.name} />
        {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
        <div className="flex gap-2">
          <Button type="submit" size="sm" variant="secondary" disabled={!changed} aria-busy={update.save.isPending} data-testid="kg-graph-save">
            {t("save")}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => remove.save.mutate(undefined)}
            aria-busy={remove.save.isPending}
            data-testid="kg-graph-delete"
          >
            {t("delete")}
          </Button>
        </div>
      </form>
    </li>
  );
}

function CreateForm({ project, sources }: { project: string; sources: string[] }) {
  const t = useTranslations("kg.graphs");
  const nameId = useId();
  const descriptionId = useId();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [held, setHeld] = useState<string[]>([]);
  const create = useGraphWrite(
    project,
    (body: { name: string; sources: string[]; description: string }) => createGraph(browserApi(), project, body),
    (body) => t("created", { name: body.name }),
    () => {
      setName("");
      setDescription("");
      setHeld([]);
    },
  );
  const next = name.trim();
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (validGraphName(next)) create.save.mutate({ name: next, sources: held, description: description.trim() });
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-2 border-t pt-3" data-testid="kg-graph-create">
      <p className="text-sm font-medium">{t("new")}</p>
      <Label htmlFor={nameId}>{t("name")}</Label>
      <Input
        id={nameId}
        value={name}
        maxLength={63}
        placeholder={t("namePlaceholder")}
        onChange={(event) => setName(event.target.value)}
        aria-invalid={next !== "" && !validGraphName(next)}
        data-testid="kg-graph-name"
      />
      <Label htmlFor={descriptionId}>{t("purpose")}</Label>
      <Input
        id={descriptionId}
        value={description}
        maxLength={MAX_GRAPH_DESCRIPTION}
        onChange={(event) => setDescription(event.target.value)}
      />
      <SourceBoxes all={sources} held={held} onToggle={(source) => setHeld((now) => toggled(now, source))} name="new" />
      {create.error ? <InlineError text={create.error.text} detail={create.error.detail} requestId={create.error.requestId} /> : null}
      <div>
        <Button type="submit" size="sm" disabled={!validGraphName(next)} aria-busy={create.save.isPending} data-testid="kg-graph-create-save">
          {t("create")}
        </Button>
      </div>
    </form>
  );
}
