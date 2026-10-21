"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ListTree } from "lucide-react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

import { useOutputViewer } from "@/components/data/output-strip";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { RunOutput } from "@/lib/outputs";

import { buildOutputIndex, type OutputIndex } from "./output-index";
import { outputsKey, type RunOutputs, runOutputsQuery } from "./output-queries";
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
/** Delete with its confirmation, loaded with the viewer it sits in. */
const DeleteOutput = dynamic(() => import("./output-delete").then((module) => module.DeleteOutput));

export const TraceOutputsContext = createContext<Pick<RunOutputsState, "index" | "open"> | null>(null);

export function useTraceOutputs() {
  return useContext(TraceOutputsContext);
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
            onClick={() => {
              show(null);
              onShowInTrace({ call, seq });
            }}
            data-testid="output-show-in-trace"
          >
            <ListTree aria-hidden="true" />
            {t("showInTrace")}
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
