"use client";

import { Eraser } from "lucide-react";
import type { Route } from "next";
import { useFormatter, useTranslations } from "next-intl";
import type { FormEvent, KeyboardEvent } from "react";

import { shortcutText } from "@/components/shell/shortcuts";
import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useModifierKey } from "@/lib/keyboard";
import { cn } from "@/lib/utils";

import { AnswerHint, CredentialFields, type useCredentialAnswer } from "./credential-decisions";
import { OptionCard } from "./decision-options";
import { type AnswerProblem, isHubDecision } from "./model";
import { type Decision, MAX_ANSWER_BYTES } from "./queries";
import { TakeOverButton } from "./take-over-button";

/** The byte count shows once an answer passes this share of the limit. */
const COUNT_FROM = 0.75;

/** The owner's answer as state, which `useAnswerForm` (decision-view.tsx) keeps and a layout renders. */
export type AnswerFormState = {
  decision: Decision;
  ids: string;
  option: string | null;
  pick: (key: string | null) => void;
  text: string;
  write: (value: string) => void;
  problem: AnswerProblem;
  bytes: number;
  long: boolean;
  parked: boolean;
  pending: boolean;
  credential: ReturnType<typeof useCredentialAnswer>;
  /** The send button's words: what a credential request's answer gives, or Send answer. */
  sendLabel: string;
  terminalHref: Route | null;
  /** The form's own props: what it submits, Cmd or Ctrl with Enter, and its name. */
  formProps: {
    onSubmit: (event: FormEvent) => void;
    onKeyDown: (event: KeyboardEvent<HTMLFormElement>) => void;
    noValidate: true;
    "aria-label": string;
    "aria-busy": true | undefined;
    "data-testid": string;
  };
};

/**
 * The answer's fields: the options as radio cards with Clear the choice, the note (or the answer itself once no option
 * is chosen) with its byte count past three quarters of the limit, and what is wrong with the answer. `screen` is the
 * phone's decision screen: options with 14 px of padding and 15 px titles, and 16 px text in the field throughout, so
 * the phone does not zoom into it.
 */
export function AnswerFields({ form, screen = false }: { form: AnswerFormState; screen?: boolean }) {
  const t = useTranslations("inbox.answer");
  const format = useFormatter();
  const { decision, ids, option, problem, bytes, long } = form;
  const problemId = `${ids}-problem`;
  const counted = bytes >= MAX_ANSWER_BYTES * COUNT_FROM;
  const describedBy = [problem ? problemId : null, counted ? `${ids}-bytes` : null].filter(Boolean).join(" ");
  return (
    <div className="flex flex-col gap-3">
      <fieldset id={`${ids}-options`} className="m-0 flex min-w-0 flex-col gap-2 border-0 p-0" data-testid="decision-form-options">
        <legend className="sr-only">{t("legend")}</legend>
        {decision.options.map((item) => (
          <OptionCard
            key={item.key}
            option={item}
            name={`decision-${decision.id}-option`}
            checked={option === item.key}
            onPick={() => form.pick(item.key)}
            screen={screen}
          />
        ))}
        {option !== null ? (
          <Button type="button" variant="ghost" size="sm" className="self-start" onClick={() => form.pick(null)} data-testid="decision-clear-option">
            <Eraser aria-hidden="true" />
            {t("clearOption")}
          </Button>
        ) : null}
      </fieldset>
      <CredentialFields form={form} screen={screen} />
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-text`} className={cn("font-medium text-muted-foreground", screen ? "text-sm" : "text-xs")}>
          {isHubDecision(form.decision) ? t("hubNote") : option !== null ? t("note") : t("text")}
        </Label>
        <Textarea
          id={`${ids}-text`}
          value={form.text}
          rows={3}
          placeholder={isHubDecision(form.decision) ? t("hubPlaceholder") : option !== null ? t("notePlaceholder") : t("textPlaceholder")}
          onChange={(event) => form.write(event.target.value)}
          className={cn(
            "max-h-64 resize-y",
            screen ? "min-h-[88px] text-base leading-[22px] md:text-base" : "min-h-[72px] md:text-[13px] md:leading-[18px]",
          )}
          aria-invalid={problem === "long" || undefined}
          aria-describedby={describedBy || undefined}
          data-testid="decision-text"
        />
        {counted ? (
          <span
            id={`${ids}-bytes`}
            className={cn("self-end text-xs tabular-nums text-muted-foreground", long && "font-medium text-danger")}
            data-testid="decision-bytes"
          >
            {t("bytes", { count: format.number(bytes), max: format.number(MAX_ANSWER_BYTES) })}
          </span>
        ) : null}
      </div>
      {problem ? (
        <p id={problemId} className="text-sm font-medium text-danger" role="alert" data-testid="decision-problem">
          {problem === "empty"
            ? t("empty")
            : problem === "option"
              ? t("option")
              : problem === "waits"
                ? t("waitsWords")
                : t("long", { max: format.number(MAX_ANSWER_BYTES) })}
        </p>
      ) : null}
    </div>
  );
}

/**
 * The card's answer form: the fields, then a footer with the hint, Take over when the owner may open the run's terminal,
 * and Send answer with its key (Cmd or Ctrl with Enter; from 768 px, where a keyboard is likely). `flush` keeps the
 * footer in view at the foot of a sheet that is the card.
 */
export function AnswerFormView({ form, flush, onTakeOver }: { form: AnswerFormState; flush: boolean; onTakeOver?: () => void }) {
  const t = useTranslations("inbox.answer");
  const modifier = useModifierKey();
  return (
    <form {...form.formProps} className="flex flex-col">
      <div className="px-4">
        <AnswerFields form={form} />
      </div>
      <div
        className={cn("flex flex-wrap items-center gap-2 p-4", flush && "sticky bottom-0 mt-3 border-t bg-popover")}
        data-testid="decision-foot"
      >
        <AnswerHint form={form} className="mr-auto min-w-0 basis-full sm:basis-auto" />
        {form.terminalHref ? <TakeOverButton decision={form.decision} href={form.terminalHref} onTakeOver={onTakeOver} /> : null}
        <Button type="submit" busy={form.pending} aria-keyshortcuts="Meta+Enter Control+Enter" data-testid="decision-send">
          {form.pending ? t("sending") : form.sendLabel}
          {modifier && !form.pending ? (
            <Kbd className="ml-0.5 border-current bg-transparent text-current opacity-70 max-md:hidden" aria-hidden="true" data-testid="decision-send-kbd">
              {shortcutText("send", modifier)}
            </Kbd>
          ) : null}
        </Button>
      </div>
    </form>
  );
}
