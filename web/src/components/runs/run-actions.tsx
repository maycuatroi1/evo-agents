"use client";

import { Ban, Check, FileDiff, Hand, Info, type LucideIcon, RotateCcw, ShieldAlert, Square, TriangleAlert, Undo2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { ConfirmAction } from "@/components/admin/confirm-action";
import { InlineError, type Notice, type WriteFailure } from "@/components/admin/notice";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { CommandLine } from "@/components/workers/command-line";
import { cn } from "@/lib/utils";

import { useRunControl, useRunFailure } from "./hooks";
import { HELD_STATES, type Run, type RunControl, runDiffHref, runHref } from "./queries";
import type { RunControls } from "./run-model";

/** The tmux session a takeover opens on the worker, and the Remote Control name Claude Code gives it (docs/workers.md). */
export function sessionName(id: number): string {
  return `evo-run-${id}`;
}

export function attachCommand(id: number): string {
  return `evo-agents worker attach ${id}`;
}

function TakeoverDialog({
  run,
  open,
  onOpenChange,
  pending,
  error,
  onConfirm,
}: {
  run: Run;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  pending: boolean;
  error: WriteFailure | null;
  onConfirm: () => void;
}) {
  const t = useTranslations("runs.detail.takeover");
  const name = sessionName(run.id);
  return (
    <Dialog open={open} onOpenChange={(next) => (pending ? undefined : onOpenChange(next))}>
      <DialogContent
        showCloseButton={false}
        className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-xl"
        onEscapeKeyDown={(event) => pending && event.preventDefault()}
        onInteractOutside={(event) => pending && event.preventDefault()}
        data-testid="takeover-dialog"
      >
        <DialogHeader>
          <DialogTitle className="text-lg font-semibold">{t("title", { id: run.id })}</DialogTitle>
          <DialogDescription className="text-pretty">{t("description")}</DialogDescription>
        </DialogHeader>
        <ol className="flex flex-col gap-4">
          <li className="grid grid-cols-[1.5rem_minmax(0,1fr)] gap-3">
            <span className="flex size-6 items-center justify-center rounded-full bg-accent text-xs font-semibold text-accent-foreground" aria-hidden="true">
              1
            </span>
            <div className="flex min-w-0 flex-col gap-1.5">
              <h3 className="text-sm font-semibold">{t("attachTitle", { worker: run.worker ?? "-" })}</h3>
              <CommandLine command={attachCommand(run.id)} label={t("attachLabel")} testId="takeover-attach" />
              <p className="text-xs text-pretty text-muted-foreground">
                {run.session_id ? t("attachHint", { name, session: run.session_id }) : t("attachHintNoSession", { name })}
              </p>
            </div>
          </li>
          {run.runtime === "claude-code" ? (
            <li className="grid grid-cols-[1.5rem_minmax(0,1fr)] gap-3">
              <span className="flex size-6 items-center justify-center rounded-full bg-accent text-xs font-semibold text-accent-foreground" aria-hidden="true">
                2
              </span>
              <div className="flex min-w-0 flex-col gap-1.5" data-testid="takeover-remote-control">
                <h3 className="text-sm font-semibold">{t("remoteTitle")}</h3>
                <p className="text-xs text-pretty text-muted-foreground">
                  {t.rich("remoteHint", { name, code: (chunks) => <code className="font-mono text-foreground">{chunks}</code> })}
                </p>
              </div>
            </li>
          ) : null}
        </ol>
        <p className="flex items-start gap-2.5 rounded-md border border-attention/20 bg-attention-soft px-3 py-2.5 text-sm text-attention">
          <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span className="text-pretty">{t("warning")}</span>
        </p>
        {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            size="lg"
            onClick={() => !pending && onOpenChange(false)}
            aria-disabled={pending || undefined}
          >
            {t("cancel")}
          </Button>
          <Button type="button" size="lg" onClick={() => !pending && onConfirm()} busy={pending} data-testid="takeover-confirm">
            <Hand aria-hidden="true" />
            {pending ? t("pending") : t("confirm")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * The run's actions in its header, as the state and the visitor's rights allow (`runControls`): Cancel (asked to
 * confirm), Take over (a dialog with the attach command and Remote Control), Hand back, Approve, Rerun, and the diff
 * once the worker uploaded one. Each control waits for the hub's answer, then says what happened in the page's notice.
 */
export function RunActions({ run, controls, onNotice }: { run: Run; controls: RunControls; onNotice: (notice: Notice) => void }) {
  const t = useTranslations("runs.detail.actions");
  const router = useRouter();
  const control = useRunControl(run);
  const failure = useRunFailure();
  const [confirming, setConfirming] = useState<"cancel" | "takeover" | null>(null);
  const [dialogError, setDialogError] = useState<WriteFailure | null>(null);
  const [acting, setActing] = useState<RunControl | null>(null);

  const act = (action: RunControl) => {
    if (control.isPending) return;
    setActing(action);
    setDialogError(null);
    control.mutate(action, {
      onSuccess: (answer) => {
        setConfirming(null);
        if (action === "rerun") {
          router.push(runHref(answer.project, answer.id));
          return;
        }
        const held = (HELD_STATES as readonly string[]).includes(answer.state);
        const text =
          action === "cancel"
            ? held
              ? t("notice.cancelAsked", { id: run.id })
              : t("notice.cancelled", { id: run.id })
            : action === "approve"
              ? t("notice.approved", { id: run.id, step: run.step_key ?? "", plan: run.plan_id })
              : action === "takeover"
                ? t("notice.takeover", { id: run.id, name: sessionName(run.id) })
                : t("notice.handback", { id: run.id });
        onNotice({ tone: "success", text });
      },
      onError: (error) => {
        const result = failure(error);
        if (confirming) setDialogError(result);
        else onNotice({ tone: "error", ...result });
      },
      onSettled: () => setActing(null),
    });
  };

  const busy = (action: RunControl) => control.isPending && acting === action;

  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="run-actions">
      {run.diff_sha256 ? (
        <Button asChild variant="outline">
          <Link href={runDiffHref(run.project, run.id)} data-testid="run-view-diff">
            <FileDiff aria-hidden="true" />
            {t("diff")}
          </Link>
        </Button>
      ) : null}
      {controls.rerun ? (
        <Button type="button" variant="outline" onClick={() => act("rerun")} aria-disabled={control.isPending || undefined} busy={busy("rerun")} data-testid="run-rerun">
          <RotateCcw aria-hidden="true" />
          {t("rerun")}
        </Button>
      ) : null}
      {controls.cancel === "offer" ? (
        <Button
          type="button"
          variant="quiet-danger"
          onClick={() => {
            setDialogError(null);
            setConfirming("cancel");
          }}
          aria-disabled={control.isPending || undefined}
          data-testid="run-cancel"
        >
          <Square aria-hidden="true" />
          {t("cancel")}
        </Button>
      ) : null}
      {controls.takeover === "offer" ? (
        <Button
          type="button"
          variant="outline"
          onClick={() => {
            setDialogError(null);
            setConfirming("takeover");
          }}
          aria-disabled={control.isPending || undefined}
          data-testid="run-takeover"
        >
          <Hand aria-hidden="true" />
          {t("takeover")}
        </Button>
      ) : null}
      {controls.handback === "offer" ? (
        <Button type="button" onClick={() => act("handback")} aria-disabled={control.isPending || undefined} busy={busy("handback")} data-testid="run-handback">
          <Undo2 aria-hidden="true" />
          {t("handback")}
        </Button>
      ) : null}
      {controls.approve ? (
        <Button type="button" onClick={() => act("approve")} aria-disabled={control.isPending || undefined} busy={busy("approve")} data-testid="run-approve">
          <Check aria-hidden="true" />
          {t("approve")}
        </Button>
      ) : null}
      <ConfirmAction
        open={confirming === "cancel"}
        onOpenChange={(open) => setConfirming(open ? "cancel" : null)}
        title={t("confirmCancel.title", { id: run.id })}
        description={
          <p className="text-pretty">
            {run.kind === "plan"
              ? (HELD_STATES as readonly string[]).includes(run.state)
                ? t("confirmCancel.planHeld")
                : t("confirmCancel.planNow")
              : (HELD_STATES as readonly string[]).includes(run.state)
                ? t("confirmCancel.held")
                : t("confirmCancel.now")}
          </p>
        }
        confirmLabel={t("confirmCancel.confirm")}
        pendingLabel={t("confirmCancel.pending")}
        cancelLabel={t("confirmCancel.keep")}
        pending={busy("cancel")}
        error={confirming === "cancel" ? dialogError : null}
        onConfirm={() => act("cancel")}
        testId="cancel-run-dialog"
      />
      <TakeoverDialog
        run={run}
        open={confirming === "takeover"}
        onOpenChange={(open) => setConfirming(open ? "takeover" : null)}
        pending={busy("takeover")}
        error={confirming === "takeover" ? dialogError : null}
        onConfirm={() => act("takeover")}
      />
    </div>
  );
}

function Note({ tone, icon: Icon, children, testId }: { tone: "info" | "warning" | "muted"; icon: LucideIcon; children: ReactNode; testId?: string }) {
  return (
    <p
      className={cn(
        "flex items-start gap-2.5 rounded-md border px-3 py-2.5 text-sm",
        tone === "info" && "bg-muted text-foreground",
        tone === "warning" && "border-attention/20 bg-attention-soft text-attention",
        tone === "muted" && "bg-card text-muted-foreground",
      )}
      data-testid={testId}
    >
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span className="min-w-0 text-pretty">{children}</span>
    </p>
  );
}

/**
 * What the run waits for, above the stepper: the owner's open asks (cancel, takeover, handback) until the worker
 * answers them, how to reach the session while the owner holds it, and, for anyone else, whose run it is.
 */
export function RunNotes({ run, controls }: { run: Run; controls: RunControls }) {
  const t = useTranslations("runs.detail.notes");
  const format = useFormatter();
  const when = (value: string) => format.dateTime(new Date(value), { timeStyle: "medium" });
  const notes: ReactNode[] = [];
  const held = (HELD_STATES as readonly string[]).includes(run.state);
  if (held && run.cancel_requested_at) {
    notes.push(
      <Note key="cancel" tone="warning" icon={Ban} testId="run-note-cancel">
        {t("cancelAsked", { time: when(run.cancel_requested_at) })}
      </Note>,
    );
  }
  if (controls.takeover === "asked" && run.takeover_requested_at) {
    notes.push(
      <Note key="takeover" tone="info" icon={Hand} testId="run-note-takeover">
        {t("takeoverAsked", { time: when(run.takeover_requested_at), worker: run.worker ?? "-" })}
      </Note>,
    );
  }
  if (controls.handback === "asked" && run.handback_requested_at) {
    notes.push(
      <Note key="handback" tone="info" icon={Undo2} testId="run-note-handback">
        {t("handbackAsked", { time: when(run.handback_requested_at) })}
      </Note>,
    );
  }
  if (controls.owner && run.state === "interactive") {
    notes.push(
      <Note key="interactive" tone="warning" icon={Hand} testId="run-note-interactive">
        {t.rich("interactive", {
          worker: run.worker ?? "-",
          command: attachCommand(run.id),
          code: (chunks) => <code className="font-mono font-medium">{chunks}</code>,
        })}
      </Note>,
    );
  }
  if (!controls.owner && !["done", "failed", "lost", "cancelled"].includes(run.state)) {
    notes.push(
      <Note key="owner" tone="muted" icon={ShieldAlert} testId="run-note-owner">
        {t("notOwner", { login: run.dispatched_by })}
      </Note>,
    );
  }
  if (controls.owner && run.state === "review") {
    notes.push(
      <Note key="review" tone="info" icon={Info} testId="run-note-review">
        {controls.approve ? t("review") : t("reviewNoWriter")}
      </Note>,
    );
  }
  return notes.length ? <div className="flex flex-col gap-2.5">{notes}</div> : null;
}
