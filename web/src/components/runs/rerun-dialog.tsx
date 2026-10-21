"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { RotateCcw } from "lucide-react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { planKeys } from "@/lib/plan-queries";

import { AccountField } from "./account-field";
import { KEEP, rerunRun } from "./account-model";
import { useRunFailure } from "./hooks";
import { type Run, runHref, runKey, runKeys } from "./queries";

/**
 * Rerun from the run's page: the run again, with the same runtime, model, mode, approval, timeout and worker, and the
 * account it takes. By default the rerun keeps the account picked by hand for the run, if one was; the visitor may
 * name another of their accounts bound to the project, or leave it to the rules. Nothing changes on screen until the
 * hub answers; then the toast links the new run and the page moves to it. A refusal stays in the dialog with the
 * hub's reason, such as an account paused since, shown by `errorView` (the page's inline error).
 */
export function RerunDialog({
  run,
  open,
  onOpenChange,
  errorView,
}: {
  run: Run;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  errorView: (failure: { text: string; detail: string | null; requestId: string | null }) => ReactNode;
}) {
  const t = useTranslations("runs.detail.rerun");
  const tToast = useTranslations("runs.detail.actions.toast");
  const router = useRouter();
  const failure = useRunFailure();
  const queryClient = useQueryClient();
  const [choice, setChoice] = useState(KEEP);
  const write = useMutation({
    mutationFn: (account: string) => rerunRun(browserApi(), run.project, run.id, account),
    onError: (error) => {
      if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
    },
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: runKey(run.project, run.id) }),
        queryClient.invalidateQueries({ queryKey: runKeys.all(run.project) }),
        queryClient.invalidateQueries({ queryKey: planKeys.one(run.project, run.plan_id) }),
      ]),
  });
  const pending = write.isPending;
  const error = write.isError ? failure(write.error) : null;
  const detail = write.isError && isApiError(write.error) && write.error.info.message ? t("hubSays", { message: write.error.info.message }) : null;

  const close = (next: boolean) => {
    if (pending) return;
    if (!next) {
      write.reset();
      setChoice(KEEP);
    }
    onOpenChange(next);
  };
  const confirm = () => {
    if (pending) return;
    write.mutate(choice, {
      onSuccess: (answer) => {
        onOpenChange(false);
        notify({
          tone: "success",
          text: tToast("rerun", { id: answer.id }),
          description: tToast("rerunText", { of: run.id }),
          link: { label: tToast("openRun"), href: runHref(answer.project, answer.id) },
        });
        router.push(runHref(answer.project, answer.id));
      },
    });
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent
        showCloseButton={false}
        className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg"
        onEscapeKeyDown={(event) => pending && event.preventDefault()}
        onInteractOutside={(event) => pending && event.preventDefault()}
        data-testid="rerun-dialog"
      >
        <DialogHeader>
          <DialogTitle className="text-lg font-semibold">{t("title", { id: run.id })}</DialogTitle>
          <DialogDescription className="text-pretty">{t("description")}</DialogDescription>
        </DialogHeader>
        <AccountField project={run.project} value={choice} onChange={setChoice} disabled={pending} rerunOf={run.id} />
        {error ? errorView({ ...error, detail: detail ?? error.detail }) : null}
        <DialogFooter>
          <Button type="button" variant="outline" size="lg" onClick={() => close(false)} aria-disabled={pending || undefined}>
            {t("cancel")}
          </Button>
          <Button type="button" size="lg" onClick={confirm} busy={pending} data-testid="rerun-confirm">
            <RotateCcw aria-hidden="true" />
            {pending ? t("pending") : t("confirm")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
