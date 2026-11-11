"use client";

import { AlarmClock, CircleStop } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { notify, notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

import { useAnswerDecision, useAnswerFailure } from "./hooks";
import { answerBody } from "./model";
import { type Decision, MAX_ANSWER_BYTES } from "./queries";

type Choice = "now" | "stop";

/**
 * What the run's owner may do about a wait (web/DESIGN.md, The wait panel): Go on now and Stop waiting, two secondary
 * buttons, since nobody needs to answer. Either opens a confirmation in place with a note for the agent, optional, and
 * sends the wait's option `now` or `stop` with it; no field for an answer of one's own is open before.
 */
export function WaitActions({
  decision,
  flush = false,
  onAnswered,
  onConflict,
}: {
  decision: Decision;
  flush?: boolean;
  onAnswered?: (decision: Decision) => void;
  onConflict?: (decision: Decision) => void;
}) {
  const t = useTranslations("wait.actions");
  const ids = useId();
  const failure = useAnswerFailure();
  const [choice, setChoice] = useState<Choice | null>(null);
  const [note, setNote] = useState("");
  const heading = useRef<HTMLHeadingElement>(null);
  const answer = useAnswerDecision(decision, {
    onAnswered: (answered) => {
      notify({ tone: "success", text: answered.answer_option === "stop" ? t("stopped") : t("woke") });
      onAnswered?.(answered);
    },
    onFailed: (error) => notifyFailure(t("failed"), failure(error, decision)),
    onConflict,
  });
  const long = new TextEncoder().encode(note.trim()).length > MAX_ANSWER_BYTES;

  useEffect(() => {
    if (choice !== null) heading.current?.focus();
  }, [choice]);

  const footer = cn("flex flex-col gap-3 border-t px-4 py-3", flush ? "sticky bottom-0 bg-card" : "");
  if (choice === null) {
    return (
      <div className={footer} data-testid="wait-actions">
        <p className="text-[13px] leading-[18px] text-pretty text-muted-foreground">{t("note")}</p>
        <div className="flex flex-wrap gap-2">
          <Button type="button" variant="secondary" onClick={() => setChoice("now")} data-testid="wait-go-on">
            <AlarmClock aria-hidden="true" />
            {t("goOn")}
          </Button>
          <Button type="button" variant="secondary" onClick={() => setChoice("stop")} data-testid="wait-stop">
            <CircleStop aria-hidden="true" />
            {t("stop")}
          </Button>
        </div>
      </div>
    );
  }
  return (
    <form
      className={footer}
      aria-labelledby={`${ids}-confirm`}
      aria-busy={answer.isPending || undefined}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        if (answer.isPending || long) return;
        answer.mutate(answerBody(choice, note));
      }}
      data-testid="wait-confirm"
      data-choice={choice}
    >
      <div className="flex flex-col gap-1">
        <h4 id={`${ids}-confirm`} ref={heading} tabIndex={-1} className="text-sm font-semibold outline-none">
          {choice === "now" ? t("confirmNow") : t("confirmStop")}
        </h4>
        <p className="text-[13px] leading-[18px] text-pretty text-muted-foreground">{choice === "now" ? t("confirmNowText") : t("confirmStopText")}</p>
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor={`${ids}-note`} className="text-[13px] font-medium">
          {t("noteLabel")}
        </label>
        <Textarea
          id={`${ids}-note`}
          value={note}
          onChange={(event) => setNote(event.target.value)}
          rows={2}
          aria-invalid={long || undefined}
          aria-describedby={long ? `${ids}-long` : undefined}
          data-testid="wait-note"
        />
        {long ? (
          <p id={`${ids}-long`} className="text-xs text-danger">
            {t("long")}
          </p>
        ) : null}
      </div>
      <div className="flex flex-wrap justify-end gap-2">
        <Button type="button" variant="ghost" onClick={() => setChoice(null)} disabled={answer.isPending} data-testid="wait-cancel">
          {t("cancel")}
        </Button>
        <Button type="submit" variant="secondary" busy={answer.isPending} data-testid="wait-send">
          {choice === "now" ? t("wake") : t("stopSend")}
        </Button>
      </div>
    </form>
  );
}
