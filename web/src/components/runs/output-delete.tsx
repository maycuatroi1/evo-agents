"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { ConfirmAction } from "@/components/feedback/confirm-action";
import { useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { baseName, type RunOutput } from "@/lib/outputs";

import { deleteRunOutput, outputsKey, type RunOutputs } from "./output-queries";
import type { Run } from "./queries";

/**
 * Delete in the viewer of a run's page, for the run's dispatcher and an admin of the project: confirmed first, then the
 * file leaves the page's list at once, a toast says so and the viewer goes on to the file beside it. A refusal stays in
 * the confirmation, in the hub's words.
 */
export function DeleteOutput({ output, run, onDeleted }: { output: RunOutput; run: Run; onDeleted: (output: RunOutput) => void }) {
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
        data-testid="output-delete"
      >
        <Trash2 aria-hidden="true" />
        {t("delete")}
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
