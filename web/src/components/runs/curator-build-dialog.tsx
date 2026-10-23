"use client";

import { Play } from "lucide-react";
import { useTranslations } from "next-intl";

import { InlineError, useWriteFailure } from "@/components/feedback/notice";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogMedia,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { isApiError } from "@/lib/api/errors";

import { useBuildCuratorPlan } from "./curator-build";
import type { Run } from "./queries";

type Props = {
  project: string;
  planId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDispatched: (run: Run) => void;
};

/**
 * Run plan on a Curator's plan: the hub queues its Builder now, beside the night shift's other runs, by the Curator's
 * rules (POST .../curator/plans/{plan}/build). Focus starts on Cancel; the dialog stays open while the hub answers, and
 * a refusal shows inside it with the hub's reason, which says what to change (resume the night shift, raise a cap,
 * wait for the plan's run).
 */
export function CuratorBuildDialog({ project, planId, open, onOpenChange, onDispatched }: Props) {
  const t = useTranslations("runs.planRun.curatorBuild");
  const failure = useWriteFailure();
  const write = useBuildCuratorPlan(project, planId);
  const pending = write.isPending;
  const guard = (event: Event) => {
    if (pending) event.preventDefault();
  };
  const error = write.isError ? failure(write.error, { 403: t("forbidden"), 404: t("notFound"), 409: t("conflict") }) : null;
  const said = write.isError && isApiError(write.error) && write.error.info.status === 409 ? t("hubSays", { message: write.error.info.message }) : null;

  const confirm = async () => {
    if (pending) return;
    let run: Run;
    try {
      run = await write.mutateAsync();
    } catch {
      return; // shown inside the dialog, from the mutation's state
    }
    onDispatched(run);
    onOpenChange(false);
  };

  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        if (pending) return;
        if (!next) write.reset();
        onOpenChange(next);
      }}
    >
      <AlertDialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-md" onEscapeKeyDown={guard} data-testid="curator-build-dialog">
        <AlertDialogHeader>
          <AlertDialogMedia className="bg-muted text-foreground">
            <Play aria-hidden="true" />
          </AlertDialogMedia>
          <AlertDialogTitle className="text-balance">{t("title")}</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="flex flex-col gap-2 text-left text-pretty">
              <p>{t.rich("body", { id: planId, plan: (chunks) => <span className="font-mono text-foreground break-all">{chunks}</span> })}</p>
              <p>{t("rules")}</p>
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        {error ? <InlineError text={error.text} detail={said ?? error.detail} requestId={error.requestId} /> : null}
        <AlertDialogFooter>
          <AlertDialogCancel size="lg" aria-disabled={pending || undefined} className="aria-disabled:opacity-50" onClick={(event) => pending && event.preventDefault()}>
            {t("cancel")}
          </AlertDialogCancel>
          <Button type="button" size="lg" busy={pending} onClick={() => void confirm()} data-testid="curator-build-confirm">
            <Play aria-hidden="true" />
            {pending ? t("pending") : t("confirm")}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
