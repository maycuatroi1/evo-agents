"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ListTree, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

import { useOutputViewer } from "@/components/data/output-strip";
import { ConfirmAction } from "@/components/feedback/confirm-action";
import { useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { baseName, type RunOutput } from "@/lib/outputs";

import { buildOutputIndex, type OutputIndex } from "./output-index";
import { deleteRunOutput, outputsKey, type RunOutputs, runOutputsQuery } from "./output-queries";
import { isActiveState, type Run, type RunEvent } from "./queries";
import type { RunViewer } from "./run-model";

/**
 * A run's outputs on its page: read from the hub, again each time the log brings the hub's note of outputs it recorded
 * or deleted (a `system` event naming `outputs` or `outputs_deleted`) and every 15 seconds while the run is active, so
 * a file the agent sends shows without a reload; where each shows in the Trace (output-index.ts); and the page's one
 * viewer, which a tile of the Outputs tab and a strip of the Trace open, with Show in trace and, for the run's
 * dispatcher and an admin of the project, Delete (confirmed first).
 */

export type TraceTarget = { call: string | null; seq: number | null };

export type RunOutputsState = {
  data: RunOutputs | undefined;
  status: "pending" | "error" | "success";
  retry: () => void;
  index: OutputIndex | null;
  /** Open the viewer on `output`, moving through `list` (the files of the strip or the grid it was opened from). */
  open: (output: RunOutput, list: readonly RunOutput[]) => void;
  viewer: ReactNode;
};

function isOutputNote(event: Pick<RunEvent, "kind" | "body">): boolean {
  const body = event.body as Record<string, unknown> | null;
  return event.kind === "system" && body !== null && typeof body === "object" && ("outputs" in body || "outputs_deleted" in body);
}

/** The Trace's outputs (`OutputIndex`) and the page's viewer, for the rows of the Trace (trace-tools.tsx, agent-trace.tsx). */
export const TraceOutputsContext = createContext<Pick<RunOutputsState, "index" | "open"> | null>(null);

export function useTraceOutputs() {
  return useContext(TraceOutputsContext);
}

function DeleteOutput({ output, run, onDeleted }: { output: RunOutput; run: Run; onDeleted: (output: RunOutput) => void }) {
  const t = useTranslations("runs.detail.outputs");
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<WriteFailure | null>(null);
  const name = baseName(output.name);
  const remove = useMutation({
    mutationFn: () => deleteRunOutput(browserApi(), run.project, run.id, output.id),
    onSuccess: () => {
      queryClient.setQueryData<RunOutputs>(outputsKey(run.project, run.id), (held) =>
        held ? { ...held, outputs: held.outputs.filter((item) => item.id !== output.id), count: held.count - 1, bytes: held.bytes - output.size } : held,
      );
      setConfirming(false);
      onDeleted(output);
      notify({ tone: "success", text: t("deleted", { name }) });
    },
    onError: (cause) => setError(failure(cause, { 403: t("deleteForbidden") })),
    onSettled: () => queryClient.invalidateQueries({ queryKey: outputsKey(run.project, run.id) }),
  });
  return (
    <>
      <Button
        type="button"
        variant="quiet-danger"
        size="sm"
        onClick={() => {
          setError(null);
          setConfirming(true);
        }}
        className="max-md:min-w-11"
        data-testid="output-delete"
      >
        <Trash2 aria-hidden="true" />
        <span className="max-md:sr-only">{t("delete")}</span>
      </Button>
      <ConfirmAction
        open={confirming}
        onOpenChange={setConfirming}
        title={t("deleteTitle", { name })}
        description={t("deleteDescription")}
        confirmLabel={t("deleteConfirm")}
        pendingLabel={t("deleting")}
        cancelLabel={t("keep")}
        pending={remove.isPending}
        error={error}
        onConfirm={() => remove.mutate()}
        testId="output-delete-confirm"
      />
    </>
  );
}

export function useRunOutputs({
  run,
  viewer,
  events,
  onShowInTrace,
}: {
  run: Run;
  viewer: RunViewer | null;
  events: readonly RunEvent[];
  onShowInTrace: (target: TraceTarget) => void;
}): RunOutputsState {
  const t = useTranslations("runs.detail.outputs");
  const queryClient = useQueryClient();
  const active = isActiveState(run.state);
  const query = useQuery(runOutputsQuery(browserApi, run.project, run.id, active));

  // The hub's notes of outputs recorded or deleted: one more than the page knew reads the outputs again.
  const notes = useMemo(() => events.reduce((count, event) => count + (isOutputNote(event) ? 1 : 0), 0), [events]);
  const known = useRef<number | null>(null);
  useEffect(() => {
    if (known.current !== null && notes > known.current) void queryClient.invalidateQueries({ queryKey: outputsKey(run.project, run.id) });
    known.current = notes;
  }, [notes, queryClient, run.project, run.id]);

  const outputs = query.data?.outputs;
  const index = useMemo(() => (outputs ? buildOutputIndex(outputs, events) : null), [outputs, events]);

  // The viewer moves through the list it was opened on, as the hub holds it now (a deleted file leaves it).
  const [listIds, setListIds] = useState<readonly number[]>([]);
  const list = useMemo(() => {
    const byId = index?.byId;
    return byId ? listIds.flatMap((id) => byId.get(id) ?? []) : [];
  }, [index, listIds]);
  const canDelete = viewer !== null && (viewer.login === run.dispatched_by || viewer.role === "admin");
  const page = useOutputViewer(list, (output, show) => {
    const call = index?.callOf.get(output.id) ?? null;
    const seq = index?.systemOf.get(output.id) ?? null;
    return (
      <>
        {call !== null || seq !== null ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="max-md:min-w-11"
            onClick={() => {
              show(null);
              onShowInTrace({ call, seq });
            }}
            data-testid="output-show-in-trace"
          >
            <ListTree aria-hidden="true" />
            <span className="max-md:sr-only">{t("showInTrace")}</span>
          </Button>
        ) : null}
        {canDelete ? (
          <DeleteOutput
            output={output}
            run={run}
            onDeleted={(gone) => {
              const at = list.findIndex((item) => item.id === gone.id);
              const next = list[at + 1] ?? list[at - 1] ?? null;
              show(next && next.id !== gone.id ? next.id : null);
            }}
          />
        ) : null}
      </>
    );
  });

  const show = page.open;
  const open = useCallback(
    (output: RunOutput, from: readonly RunOutput[]) => {
      setListIds(from.map((item) => item.id));
      show(output.id);
    },
    [show],
  );
  return {
    data: query.data,
    status: query.isPending ? "pending" : query.isError ? "error" : "success",
    retry: () => void query.refetch(),
    index,
    open,
    viewer: page.viewer,
  };
}
