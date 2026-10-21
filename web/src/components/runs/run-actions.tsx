"use client";

import { Check, Ellipsis, FileDiff, Hand, type LucideIcon, RotateCcw, Square, TriangleAlert, Undo2 } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { Fragment, type ReactNode, useState } from "react";

import { ConfirmAction } from "@/components/admin/confirm-action";
import { InlineError, type WriteFailure } from "@/components/admin/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { stepHref } from "@/components/plans/links";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { CommandLine } from "@/components/workers/command-line";
import { useIsMobile } from "@/hooks/use-mobile";

import { useRunControl, useRunFailure } from "./hooks";
import { HELD_STATES, type Run, type RunControl, runDiffHref } from "./queries";
import { RerunDialog } from "./rerun-dialog";
import { readDiffstat, type RunControls } from "./run-model";

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
 * The run's actions in its header, as the state and the visitor's rights allow (`runControls`), in the kit's order:
 * Take over (a dialog with the attach command and Remote Control), the diff once the worker uploaded one (with its
 * `+12 −3` when the worker reported a diffstat), Rerun (a dialog with the account it takes), Hand back, Approve, and
 * Cancel last (asked to confirm). At most one of them is a solid button (Approve or Hand back) and one an outline button
 * (Take over, the diff or Rerun); the others are items of a menu behind `Ellipsis`, Cancel last in `danger`. Each
 * control waits for the hub's answer, then says what happened in a toast; a failure inside a dialog stays there, any
 * other stays as a toast until dismissed.
 */

type Action = {
  id: RunControl | "diff" | "rerun";
  label: ReactNode;
  icon: LucideIcon;
  testId: string;
  onSelect?: () => void;
  href?: Route;
  busy?: boolean;
  destructive?: boolean;
};
export function RunActions({
  run,
  controls,
  children,
}: {
  run: Run;
  controls: RunControls;
  /** Where the header puts them: the buttons under the run's progress, the menu at the end of its title line. */
  children: (parts: { buttons: ReactNode; menu: ReactNode }) => ReactNode;
}) {
  const t = useTranslations("runs.detail.actions");
  const format = useFormatter();
  const control = useRunControl(run);
  const failure = useRunFailure();
  const [confirming, setConfirming] = useState<"cancel" | "takeover" | null>(null);
  const [dialogError, setDialogError] = useState<WriteFailure | null>(null);
  const [acting, setActing] = useState<RunControl | null>(null);
  const [rerunning, setRerunning] = useState(false);
  const mobile = useIsMobile();

  const act = (action: RunControl) => {
    if (control.isPending) return;
    setActing(action);
    setDialogError(null);
    control.mutate(action, {
      onSuccess: (answer) => {
        setConfirming(null);
        const held = (HELD_STATES as readonly string[]).includes(answer.state);
        if (action === "cancel") {
          notify(
            held
              ? { tone: "success", text: t("toast.cancelAsked", { id: run.id }), description: t("toast.cancelAskedText") }
              : { tone: "success", text: t("toast.cancelled", { id: run.id }) },
          );
        } else if (action === "approve") {
          notify({
            tone: "success",
            text: t("toast.approved", { id: run.id }),
            description: t("toast.approvedText", { step: run.step_key ?? "", plan: run.plan_id }),
            link: run.step_key ? { label: t("toast.openStep"), href: stepHref(run.project, run.plan_id, run.step_key) } : null,
          });
        } else if (action === "takeover") {
          notify({ tone: "success", text: t("toast.takeover", { id: run.id }), description: t("toast.takeoverText", { name: sessionName(run.id) }) });
        } else {
          notify({ tone: "success", text: t("toast.handback", { id: run.id }), description: t("toast.handbackText") });
        }
      },
      onError: (error) => {
        const result = failure(error);
        if (confirming) setDialogError(result);
        else notifyFailure(t(`toast.failed.${action}`, { id: run.id }), result);
      },
      onSettled: () => setActing(null),
    });
  };

  const busy = (action: RunControl) => control.isPending && acting === action;
  const diffstat = readDiffstat(run.diffstat);

  const actions: Action[] = [];
  if (controls.takeover === "offer") {
    actions.push({
      id: "takeover",
      label: t("takeover"),
      icon: Hand,
      testId: "run-takeover",
      onSelect: () => {
        setDialogError(null);
        setConfirming("takeover");
      },
    });
  }
  if (run.diff_sha256) {
    actions.push({
      id: "diff",
      label: (
        <>
          {t("diff")}
          {diffstat ? (
            <>
              <span className="font-mono text-xs tabular-nums" aria-hidden="true" data-testid="run-view-diff-stat">
                <span className="text-success">+{format.number(diffstat.insertions)}</span> <span className="text-danger">−{format.number(diffstat.deletions)}</span>
              </span>
              <span className="sr-only">, {t("diffstat", { insertions: diffstat.insertions, deletions: diffstat.deletions })}</span>
            </>
          ) : null}
        </>
      ),
      icon: FileDiff,
      testId: "run-view-diff",
      href: runDiffHref(run.project, run.id),
    });
  }
  if (controls.rerun) actions.push({ id: "rerun", label: t("rerun"), icon: RotateCcw, testId: "run-rerun", onSelect: () => setRerunning(true) });
  if (controls.handback === "offer") actions.push({ id: "handback", label: t("handback"), icon: Undo2, testId: "run-handback", onSelect: () => act("handback"), busy: busy("handback") });
  if (controls.approve) actions.push({ id: "approve", label: t("approve"), icon: Check, testId: "run-approve", onSelect: () => act("approve"), busy: busy("approve") });
  if (controls.cancel === "offer") {
    actions.push({
      id: "cancel",
      label: t("cancel"),
      icon: Square,
      testId: "run-cancel",
      destructive: true,
      onSelect: () => {
        setDialogError(null);
        setConfirming("cancel");
      },
    });
  }
  // At most one solid control and one outline one, in the kit's order; the rest, Cancel last, in the menu.
  const solid = actions.find((action) => action.id === "approve" || action.id === "handback") ?? null;
  // A phone takes over from a computer: there Take over is in the menu.
  const outline = actions.find((action) => action !== solid && ((action.id === "takeover" && !mobile) || action.id === "diff" || action.id === "rerun")) ?? null;
  const rest = actions.filter((action) => action !== solid && action !== outline);
  const pending = control.isPending || undefined;
  const button = (action: Action, variant: "default" | "outline") =>
    action.href ? (
      <Button key={action.id} asChild variant={variant} className="max-md:h-11">
        <Link href={action.href} data-testid={action.testId}>
          <action.icon aria-hidden="true" />
          {action.label}
        </Link>
      </Button>
    ) : (
      <Button key={action.id} type="button" variant={variant} className="max-md:h-11" onClick={action.onSelect} aria-disabled={pending} busy={action.busy} data-testid={action.testId}>
        <action.icon aria-hidden="true" />
        {action.label}
      </Button>
    );
  const buttons =
    solid || outline ? (
      <div className="flex flex-wrap items-center gap-2" data-testid="run-actions">
        {solid ? button(solid, "default") : null}
        {outline ? button(outline, "outline") : null}
      </div>
    ) : null;
  const menu = rest.length ? (
    // Not modal, as the hub's other menus (user-menu.tsx): a modal menu hides the page from assistive tech while its
    // controls stay focusable.
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <Button type="button" variant="ghost" size="icon" aria-label={t("more")} data-testid="run-actions-menu">
          <Ellipsis aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-48">
        {rest.map((action, index) => (
          <Fragment key={action.id}>
            {action.destructive && index > 0 ? <DropdownMenuSeparator /> : null}
            {action.href ? (
              <DropdownMenuItem asChild>
                <Link href={action.href} data-testid={action.testId}>
                  <action.icon aria-hidden="true" />
                  {action.label}
                </Link>
              </DropdownMenuItem>
            ) : (
              <DropdownMenuItem
                variant={action.destructive ? "destructive" : "default"}
                disabled={control.isPending}
                onSelect={action.onSelect}
                data-testid={action.testId}
              >
                <action.icon aria-hidden="true" />
                {action.label}
              </DropdownMenuItem>
            )}
          </Fragment>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  ) : null;

  return (
    <>
      {children({ buttons, menu })}
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
      {controls.rerun ? <RerunDialog run={run} open={rerunning} onOpenChange={setRerunning} errorView={(error) => <InlineError {...error} />} /> : null}
      <TakeoverDialog
        run={run}
        open={confirming === "takeover"}
        onOpenChange={(open) => setConfirming(open ? "takeover" : null)}
        pending={busy("takeover")}
        error={confirming === "takeover" ? dialogError : null}
        onConfirm={() => act("takeover")}
      />
    </>
  );
}
