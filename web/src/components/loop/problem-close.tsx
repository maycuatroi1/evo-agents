"use client";

import { CircleCheck, CircleMinus, RotateCcw } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import type { ProjectProblem } from "@/lib/problem-queries";
import { CLOSED_STATUSES, closeProblem, PROBLEM_LIMITS, reopening, writeProblem } from "@/lib/problem-writes";

import { useProblemWrite } from "./problem-hooks";

type Closed = (typeof CLOSED_STATUSES)[number];
type Written = Awaited<ReturnType<typeof writeProblem>>;

const CLOSE_ICONS = { solved: CircleCheck, dropped: CircleMinus } as const;

function CloseForm({ project, problem, onClose }: { project: string; problem: ProjectProblem; onClose: () => void }) {
  const t = useTranslations("problems.close");
  const words = useTranslations("problems.status");
  const ids = useId();
  const [status, setStatus] = useState<Closed>("solved");
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [tried, setTried] = useState(false);
  const { busy, refused, run } = useProblemWrite<Written>(project);
  const missing = tried && !reason.trim();
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTried(true);
    if (!reason.trim() || busy) return;
    void run(
      () => closeProblem(browserApi(), project, problem.code, { status, reason: reason.trim(), note: note.trim() || null, if_revision: problem.revision }),
      (written) => {
        notify({ tone: "success", text: t("done", { code: written.code, status: written.status }) });
        onClose();
      },
      { 409: t("conflict", { code: problem.code }), 422: t("refused") },
    );
  };
  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-5" data-testid="problem-close-form">
      <DialogHeader>
        <DialogTitle>{t("title", { code: problem.code })}</DialogTitle>
        <DialogDescription>{t("text")}</DialogDescription>
      </DialogHeader>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1.5 text-sm font-medium">{t("how")}</legend>
        <RadioGroup value={status} onValueChange={(value) => setStatus(value === "dropped" ? "dropped" : "solved")} name="status" data-testid="problem-close-status">
          {CLOSED_STATUSES.map((value) => {
            const Icon = CLOSE_ICONS[value];
            return (
              <Label
                key={value}
                htmlFor={`${ids}-${value}`}
                className="flex cursor-pointer items-start gap-3 rounded-md border px-3 py-2.5 font-normal transition-colors hover:bg-accent has-data-checked:border-brand/40 has-data-checked:bg-surface-selected"
              >
                <RadioGroupItem id={`${ids}-${value}`} value={value} className="mt-0.5" aria-labelledby={`${ids}-${value}-name`} aria-describedby={`${ids}-${value}-hint`} data-testid={`problem-close-${value}`} />
                <span className="flex flex-col gap-1">
                  <span id={`${ids}-${value}-name`} className="inline-flex items-center gap-1.5 text-sm font-medium">
                    <Icon className="size-3.5 text-muted-foreground" aria-hidden="true" />
                    {words(value)}
                  </span>
                  <span id={`${ids}-${value}-hint`} className="text-xs leading-snug text-muted-foreground">
                    {t(`hints.${value}`)}
                  </span>
                </span>
              </Label>
            );
          })}
        </RadioGroup>
      </fieldset>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-reason`}>
          {t("reason")}
          <span className="font-normal text-fg-subtle">{t("required")}</span>
        </Label>
        <Textarea
          id={`${ids}-reason`}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          rows={3}
          maxLength={PROBLEM_LIMITS.reason}
          aria-required="true"
          aria-invalid={missing || undefined}
          aria-describedby={`${ids}-reason-${missing ? "error" : "help"}`}
          data-testid="problem-close-reason-input"
        />
        {missing ? (
          <p id={`${ids}-reason-error`} className="text-xs font-medium text-danger">
            {t("reasonMissing")}
          </p>
        ) : (
          <p id={`${ids}-reason-help`} className="text-xs text-fg-subtle">
            {t("reasonHelp")}
          </p>
        )}
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-note`}>{t("note")}</Label>
        <Input id={`${ids}-note`} value={note} onChange={(event) => setNote(event.target.value)} maxLength={PROBLEM_LIMITS.note} />
      </div>
      {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
      <DialogFooter>
        <DialogClose asChild>
          <Button type="button" variant="outline" size="lg">
            {t("cancel")}
          </Button>
        </DialogClose>
        <Button type="submit" size="lg" busy={busy} data-testid="problem-close-submit">
          {busy ? t("closing") : t("submit", { status })}
        </Button>
      </DialogFooter>
    </form>
  );
}

/** A writer closes an open problem as solved or dropped, saying why; it stays, with its reason, and may be reopened. */
export function CloseProblemDialog({ project, problem, open, onOpenChange }: { project: string; problem: ProjectProblem; open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg" data-testid="problem-close-dialog">
        {open ? <CloseForm project={project} problem={problem} onClose={() => onOpenChange(false)} /> : null}
      </DialogContent>
    </Dialog>
  );
}

function ReopenForm({ project, problem, onClose }: { project: string; problem: ProjectProblem; onClose: () => void }) {
  const t = useTranslations("problems.reopen");
  const ids = useId();
  const [note, setNote] = useState("");
  const { busy, refused, run } = useProblemWrite<Written>(project);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    void run(
      () => writeProblem(browserApi(), project, problem.code, reopening(problem, note)),
      (written) => {
        notify({ tone: "success", text: t("done", { code: written.code }) });
        onClose();
      },
      { 409: t("conflict", { code: problem.code }), 422: t("refused") },
    );
  };
  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-5" data-testid="problem-reopen-form">
      <DialogHeader>
        <DialogTitle>{t("title", { code: problem.code })}</DialogTitle>
        <DialogDescription>{t("text", { status: problem.status, reason: problem.close_reason ?? "" })}</DialogDescription>
      </DialogHeader>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-note`}>{t("note")}</Label>
        <Input id={`${ids}-note`} value={note} onChange={(event) => setNote(event.target.value)} maxLength={PROBLEM_LIMITS.note} data-testid="problem-reopen-note" />
      </div>
      {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
      <DialogFooter>
        <DialogClose asChild>
          <Button type="button" variant="outline" size="lg">
            {t("cancel")}
          </Button>
        </DialogClose>
        <Button type="submit" size="lg" busy={busy} data-testid="problem-reopen-submit">
          <RotateCcw aria-hidden="true" />
          {busy ? t("reopening") : t("submit")}
        </Button>
      </DialogFooter>
    </form>
  );
}

/** A writer reopens a solved or dropped problem: the same problem, open again, without the reason it was closed. */
export function ReopenProblemDialog({ project, problem, open, onOpenChange }: { project: string; problem: ProjectProblem; open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-md" data-testid="problem-reopen-dialog">
        {open ? <ReopenForm project={project} problem={problem} onClose={() => onOpenChange(false)} /> : null}
      </DialogContent>
    </Dialog>
  );
}
