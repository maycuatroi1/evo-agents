"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CirclePause, CirclePlay } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { ConfirmAction } from "@/components/feedback/confirm-action";
import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import { opsKeys, pauseOps } from "@/lib/system-ops-queries";

/** Pause the ops of a system with a reason, or resume them; the ops panel reads them again after either. */
function usePause(project: string, slug: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (reason: string | null) => pauseOps(browserApi(), project, slug, reason),
    onSettled: () => queryClient.invalidateQueries({ queryKey: opsKeys.ops(project, slug) }),
  });
}

/**
 * Pause the ops of a system, for an admin, after a dialog that asks why: paused, the hub still queues the project's
 * operations events, but opens no ops thread for them until someone resumes. The reason, optional, shows on the page;
 * without one the hub writes who paused them.
 */
export function PauseOps({ project, slug }: { project: string; slug: string }) {
  const t = useTranslations("systems.ops.pause");
  const id = useId();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const write = usePause(project, slug);
  const failure = useWriteFailure();
  const error = write.isError && open ? failure(write.error, { 403: t("forbidden"), 409: t("conflict") }) : null;
  const confirm = async () => {
    try {
      await write.mutateAsync(reason.trim());
      setOpen(false);
      setReason("");
      notify({ tone: "success", text: t("done"), description: t("doneText") });
    } catch {
      // The reason shows in the dialog, which stays open.
    }
  };
  return (
    <>
      <Button
        type="button"
        variant="secondary"
        size="sm"
        className="max-md:min-h-11"
        onClick={() => {
          write.reset();
          setOpen(true);
        }}
        data-testid="ops-pause"
      >
        <CirclePause aria-hidden="true" />
        {t("pause")}
      </Button>
      <ConfirmAction
        open={open}
        onOpenChange={setOpen}
        title={t("confirmTitle")}
        description={
          <>
            <p>{t("confirmText")}</p>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={id} className="text-foreground">
                {t("reason")}
              </Label>
              <Textarea
                id={id}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                maxLength={200}
                rows={3}
                aria-describedby={`${id}-hint`}
                data-testid="ops-pause-reason"
              />
              <p id={`${id}-hint`} className="text-xs text-muted-foreground">
                {t("reasonHint")}
              </p>
            </div>
          </>
        }
        confirmLabel={t("pause")}
        pendingLabel={t("pausing")}
        cancelLabel={t("cancel")}
        pending={write.isPending}
        error={error}
        onConfirm={() => void confirm()}
        testId="ops-pause-dialog"
      />
    </>
  );
}

/** Resume the ops of a system, for an admin: the queue opens threads again at once, so it needs no dialog. */
export function ResumeOps({ project, slug }: { project: string; slug: string }) {
  const t = useTranslations("systems.ops.pause");
  const write = usePause(project, slug);
  const failure = useWriteFailure();
  return (
    <Button
      type="button"
      size="sm"
      className="max-md:min-h-11"
      busy={write.isPending}
      onClick={() =>
        write.mutate(null, {
          onSuccess: () => notify({ tone: "success", text: t("resumed"), description: t("resumedText") }),
          onError: (error) => notifyFailure(t("resumeFailed"), failure(error, { 403: t("forbidden") })),
        })
      }
      data-testid="ops-resume"
    >
      <CirclePlay aria-hidden="true" />
      {t("resume")}
    </Button>
  );
}
