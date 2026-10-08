"use client";

import { CalendarClock, Check, ThumbsDown } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError, type WriteFailure } from "@/components/admin/notice";
import { notify } from "@/components/feedback/toast";
import { ShortcutKeys } from "@/components/shell/shortcuts";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { isSendShortcut } from "@/lib/keyboard";

import { useAnswerProposal, useCuratorFailure } from "./hooks";
import { answerBody, answerProblem, DEFER_DAYS, isAnswerable, MAX_NOTE_CHARS } from "./model";
import { type Proposal, type ProposalAction, proposalHref } from "./queries";

export const ACTION_ICONS = { accept: Check, defer: CalendarClock, reject: ThumbsDown } as const;

/** The answer buttons of an admin, in the kit's order: Accept (the page's primary action), Defer, Reject. */
export function AnswerButtons({
  proposal,
  onAnswer,
  size = "default",
}: {
  proposal: Pick<Proposal, "id" | "state">;
  onAnswer: (action: ProposalAction) => void;
  size?: "default" | "sm";
}) {
  const t = useTranslations("curator.answer");
  if (!isAnswerable(proposal)) return null;
  return (
    <>
      {(["accept", "defer", "reject"] as const).map((action) => {
        const Icon = ACTION_ICONS[action];
        return (
          <Button
            key={action}
            type="button"
            size={size}
            variant={action === "accept" ? "default" : "secondary"}
            onClick={() => onAnswer(action)}
            aria-label={t(`buttonLabel.${action}`, { id: proposal.id })}
            data-testid={`proposal-${action}`}
          >
            <Icon aria-hidden="true" />
            {t(`button.${action}`)}
          </Button>
        );
      })}
    </>
  );
}

/**
 * The confirm step of an answer: what accepting, deferring or rejecting does, an optional note of one line for the
 * record, and for a deferral its days (1 to 90, 7 at first). Cmd or Ctrl with Enter sends from anywhere in it. The
 * dialog stays open while the answer goes out and shows a refusal in place; once the hub answers, a toast says so
 * and the dialog closes. After a 409 (answered meanwhile) the proposal shown behind it is the one the hub holds.
 */
export function AnswerDialog({
  project,
  proposal,
  action,
  onClose,
  linkBack = false,
}: {
  project: string;
  proposal: Pick<Proposal, "id" | "title">;
  action: ProposalAction | null;
  onClose: () => void;
  /** Whether the toast links to the proposal's page (from the Inbox), or not (on that page). */
  linkBack?: boolean;
}) {
  const t = useTranslations("curator.answer");
  const ids = useId();
  const failure = useCuratorFailure();
  const [shown, setShown] = useState<ProposalAction>(action ?? "accept");
  if (action !== null && action !== shown) setShown(action);
  const [note, setNote] = useState("");
  const [days, setDays] = useState(String(DEFER_DAYS.default));
  const [checked, setChecked] = useState(false);
  const [error, setError] = useState<WriteFailure | null>(null);
  const mutation = useAnswerProposal(project, proposal.id);
  const problem = answerProblem(shown, note, days);
  const Icon = ACTION_ICONS[shown];

  const close = (open: boolean) => {
    if (open || mutation.isPending) return;
    setNote("");
    setDays(String(DEFER_DAYS.default));
    setChecked(false);
    setError(null);
    onClose();
  };

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    setChecked(true);
    if (problem || mutation.isPending) return;
    setError(null);
    mutation.mutate(answerBody(shown, note, days), {
      onSuccess: () => {
        notify({
          tone: "success",
          text: t(`done.${shown}`, { id: proposal.id }),
          description: t(`doneText.${shown}`, { days: Number(days) }),
          link: linkBack ? { label: t("openProposal"), href: proposalHref(project, proposal.id) } : null,
        });
        setNote("");
        setChecked(false);
        onClose();
      },
      onError: (failed) => setError(failure(failed, "answer")),
    });
  };

  const fieldError = checked ? problem : null;
  return (
    <Dialog open={action !== null} onOpenChange={close}>
      <DialogContent className="sm:max-w-lg" data-testid="proposal-answer-dialog" data-action={shown}>
        <form
          noValidate
          onSubmit={submit}
          onKeyDown={(event) => {
            if (isSendShortcut(event.nativeEvent)) {
              event.preventDefault();
              submit();
            }
          }}
          className="grid gap-4"
        >
          <DialogHeader>
            <DialogTitle className="pr-8 text-balance break-words">{t(`title.${shown}`, { id: proposal.id })}</DialogTitle>
            <DialogDescription className="text-pretty">{t(`text.${shown}`)}</DialogDescription>
          </DialogHeader>
          <p className="rounded-sm bg-surface-sunken px-3 py-2 text-[13px] text-pretty text-foreground [overflow-wrap:anywhere]">{proposal.title}</p>
          {shown === "defer" ? (
            <div className="grid gap-1.5">
              <Label htmlFor={`${ids}-days`}>{t("days")}</Label>
              <Input
                id={`${ids}-days`}
                type="number"
                inputMode="numeric"
                min={DEFER_DAYS.min}
                max={DEFER_DAYS.max}
                value={days}
                onChange={(event) => setDays(event.target.value)}
                className="w-28"
                aria-invalid={fieldError === "days" || undefined}
                aria-describedby={`${ids}-days-hint`}
                data-testid="proposal-defer-days"
              />
              <p id={`${ids}-days-hint`} className={fieldError === "days" ? "text-xs text-danger" : "text-xs text-fg-subtle"}>
                {fieldError === "days" ? t("errors.days") : t("daysHint")}
              </p>
            </div>
          ) : null}
          <div className="grid gap-1.5">
            <Label htmlFor={`${ids}-note`}>{t("note")}</Label>
            <Textarea
              id={`${ids}-note`}
              value={note}
              onChange={(event) => setNote(event.target.value)}
              rows={2}
              maxLength={MAX_NOTE_CHARS + 200}
              placeholder={t("notePlaceholder")}
              aria-invalid={fieldError === "noteLine" || fieldError === "noteLong" || undefined}
              aria-describedby={`${ids}-note-hint`}
              data-testid="proposal-answer-note"
            />
            <p
              id={`${ids}-note-hint`}
              className={fieldError === "noteLine" || fieldError === "noteLong" ? "text-xs text-danger" : "text-xs text-fg-subtle"}
            >
              {fieldError === "noteLine" ? t("errors.noteLine") : fieldError === "noteLong" ? t("errors.noteLong") : t("noteHint")}
            </p>
          </div>
          {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
          <DialogFooter>
            <Button type="button" variant="outline" size="lg" onClick={() => close(false)} aria-disabled={mutation.isPending || undefined}>
              {t("cancel")}
            </Button>
            <Button
              type="submit"
              size="lg"
              variant={shown === "accept" ? "default" : "secondary"}
              busy={mutation.isPending}
              aria-keyshortcuts="Meta+Enter Control+Enter"
              data-testid="proposal-answer-send"
            >
              <Icon aria-hidden="true" />
              {mutation.isPending ? t(`sending.${shown}`) : t(`send.${shown}`)}
              <ShortcutKeys id="send" className="ml-1" keyClassName="border-current bg-transparent text-current opacity-70" />
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
