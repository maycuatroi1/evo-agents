"use client";

import { ChevronDown, CircleCheck, Clock, Eraser, Lock, Terminal } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, type KeyboardEvent, type ReactNode, type Ref, useEffect, useId, useRef, useState } from "react";

import { notify, notifyFailure } from "@/components/feedback/toast";
import { useNow } from "@/components/kg/use-now";
import { SafeMarkdown } from "@/components/memories/markdown";
import { planHref, stepHref } from "@/components/plans/links";
import { Prose } from "@/components/plans/prose";
import { durationParts } from "@/components/runs/model";
import { runHref } from "@/components/runs/queries";
import { utf8Bytes } from "@/components/runs/run-model";
import { shortcutText } from "@/components/shell/shortcuts";
import { StatusBadge, useStatusText } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Ago } from "@/components/workers/ago";
import { isSendShortcut, useModifierKey } from "@/lib/keyboard";
import { cn } from "@/lib/utils";

import { AgentPickTag, DecisionCategoryBadge } from "./badges";
import { useAnswerDecision, useAnswerFailure, useDecisionTerminalHref, useInboxViewer, useParksAt } from "./hooks";
import {
  type AnswerAccess,
  answerAccess,
  answerBody,
  answerProblem,
  type AnswerProblem,
  chosenOption,
  initialOption,
  parkTiming,
} from "./model";
import { type Decision, type DecisionOption, MAX_ANSWER_BYTES } from "./queries";

/**
 * The kit's DecisionCard (web/DESIGN.md, components/inbox): an agent's question to the owner of its plan run, the
 * context it gave, the options with the agent's pick chosen, and one answer that lets the agent go on. The same card
 * sits in the Inbox's sheet, in the sheet Home opens, and in the side column of a plan run's page.
 */

/** The byte count shows once an answer passes this share of the limit. */
const COUNT_FROM = 0.75;

type HeadingLevel = 2 | 3;

/** A link in the card's head: text colour with a quiet underline, cobalt under the pointer. */
const HEAD_LINK =
  "rounded-xs font-medium text-foreground underline decoration-foreground/30 underline-offset-2 hover:text-brand hover:decoration-current";
const LINK = "text-brand underline-offset-4 hover:text-brand-hover hover:underline";

function useShortDuration() {
  const t = useTranslations("runs.duration");
  return (ms: number) => {
    const { hours, minutes } = durationParts(ms);
    if (hours > 0) return t("hours", { hours, minutes });
    return minutes > 0 ? t("minutes", { minutes }) : t("lessThanMinute");
  };
}

/** "asked 6 minutes ago, parks in 23 h 54 min", or "parked 3 hours ago" once the run has parked. */
export function AskedTiming({ decision, parksAt, className = "ml-auto" }: { decision: Decision; parksAt: string | null; className?: string }) {
  const t = useTranslations("inbox.decision");
  const duration = useShortDuration();
  const counting = decision.state === "open" && decision.run_state === "waiting" && parksAt !== null;
  const now = useNow(counting);
  const timing = parkTiming(decision, parksAt, now);
  return (
    <span className={cn("inline-flex flex-wrap items-center gap-x-1 tabular-nums", className)} data-testid="decision-timing">
      <span>
        {t.rich("asked", { time: () => <Ago value={decision.asked_at} never="-" /> })}
        {timing ? "," : null}
      </span>
      {timing?.kind === "parksIn" ? (
        <span data-testid="decision-parks">{t("parksIn", { duration: duration(timing.ms) })}</span>
      ) : timing?.kind === "parked" ? (
        <span data-testid="decision-parks">{t.rich("parked", { time: () => <Ago value={timing.at} never="-" /> })}</span>
      ) : null}
    </span>
  );
}

/** The decision's state as the kit's pill: Waiting for you for the run's owner, Waiting for its owner for anyone else. */
export function DecisionStatePill({ decision, access, size }: { decision: Decision; access: AnswerAccess; size?: "lg" }) {
  const t = useTranslations("inbox.decision");
  const label =
    decision.state === "open" ? (access === "answer" ? t("waitingYou") : access === "notOwner" ? t("waitingFor", { owner: decision.owner }) : undefined) : undefined;
  return <StatusBadge kind="decision" status={decision.state} label={label} size={size} />;
}

/**
 * The card's head: the decision's state as the kit's pill (Waiting for you, for the run's owner), its category, where
 * it comes from (the run, the project, plan and step; on the run's own page only the step), and when it was asked and
 * when the run parks, or who answered and when. Amber while it waits for a person, sunken once it is closed.
 */
function DecisionHead({ decision, where, access, parksAt }: { decision: Decision; where: "inbox" | "run"; access: AnswerAccess; parksAt: string | null }) {
  const t = useTranslations("inbox.decision");
  const open = decision.state === "open";
  const step =
    decision.step_key !== null ? (
      <Link href={stepHref(decision.project, decision.plan_id, decision.step_key)} className={HEAD_LINK} data-testid="decision-step-link">
        {t("stepLink", { key: decision.step_key })}
      </Link>
    ) : null;
  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-x-2 gap-y-1.5 px-4 py-2.5 text-xs text-muted-foreground",
        open ? "bg-attention-soft" : "bg-surface-sunken",
      )}
      data-testid="decision-head"
    >
      <DecisionStatePill decision={decision} access={access} />
      <DecisionCategoryBadge category={decision.category} />
      {where === "inbox" ? (
        <>
          <Link href={runHref(decision.project, decision.run_id)} className={cn(HEAD_LINK, "tabular-nums")} data-testid="decision-run-link">
            {t("runLink", { id: decision.run_id })}
          </Link>
          <span className="inline-flex min-w-0 flex-wrap items-center gap-x-1 [overflow-wrap:anywhere]" data-testid="decision-where">
            <span className="font-mono">{decision.project}</span>
            <span aria-hidden="true">/</span>
            <span>
              <Link href={planHref(decision.project, decision.plan_id)} className={cn(HEAD_LINK, "font-mono font-normal")} data-testid="decision-plan-link">
                {decision.plan_id}
              </Link>
              {step ? "," : null}
            </span>
            {step}
          </span>
        </>
      ) : (
        step
      )}
      {decision.state === "answered" ? (
        <span className="ml-auto" data-testid="decision-answered-by">
          {t.rich("answeredBy", {
            login: decision.answered_by ?? decision.owner,
            who: (chunks) => <span className="font-mono text-foreground">{chunks}</span>,
          })}
          {decision.answered_at ? (
            <>
              {","} <Ago value={decision.answered_at} never="-" />
            </>
          ) : null}
        </span>
      ) : open ? (
        <AskedTiming decision={decision} parksAt={parksAt} />
      ) : null}
    </div>
  );
}

function Question({
  level,
  id,
  headingRef,
  quiet,
  children,
}: {
  level: HeadingLevel;
  id: string;
  headingRef?: Ref<HTMLHeadingElement>;
  quiet: boolean;
  children: ReactNode;
}) {
  const Tag = level === 2 ? "h2" : "h3";
  return (
    <Tag
      id={id}
      ref={headingRef}
      tabIndex={headingRef ? -1 : undefined}
      className={cn(
        "rounded-xs whitespace-pre-line text-pretty outline-none [overflow-wrap:anywhere] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
        quiet ? "text-[13px] leading-[19px] font-normal text-muted-foreground" : "text-[15px] leading-[22px] font-medium text-foreground",
      )}
    >
      {children}
    </Tag>
  );
}

/**
 * The agent's context, Markdown rendered without raw HTML or images (the memories' `SafeMarkdown`), folded after four
 * lines with a button for the rest. Focus moving into the folded part (a link, a code block) unfolds it. On the phone's
 * screen (`screen`) it reads at 15 px.
 */
export function FoldedContext({ text, screen = false }: { text: string; screen?: boolean }) {
  const t = useTranslations("inbox.decision");
  const ids = useId();
  const box = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [overflows, setOverflows] = useState(false);

  useEffect(() => {
    const element = box.current;
    if (!element || open) return;
    const measure = () => setOverflows(element.scrollHeight - element.clientHeight > 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [open, text]);

  return (
    <div className="flex flex-col items-start gap-1" data-testid="decision-context" data-folded={!open && overflows ? "true" : "false"}>
      <div
        id={`${ids}-context`}
        ref={box}
        role="group"
        aria-label={t("context")}
        className={cn("w-full", screen ? "text-[15px] leading-[22px]" : "text-[13px] leading-relaxed", !open && "line-clamp-4")}
        onFocus={() => {
          if (!open && overflows) setOpen(true);
        }}
      >
        <SafeMarkdown testId="decision-context-markdown" className={cn("text-muted-foreground", screen ? "text-[15px] leading-[22px]" : "text-[13px]")}>
          {text}
        </SafeMarkdown>
      </div>
      {overflows || open ? (
        <Button
          type="button"
          variant="link"
          size="sm"
          className={cn("h-auto min-h-6 px-0", screen ? "text-sm max-md:min-h-11" : "text-xs")}
          aria-expanded={open}
          aria-controls={`${ids}-context`}
          onClick={() => setOpen(!open)}
          data-testid="decision-context-toggle"
        >
          <ChevronDown className={cn("transition-transform duration-base motion-reduce:transition-none", open && "rotate-180")} aria-hidden="true" />
          {open ? t("showLess") : t("showMore")}
        </Button>
      ) : null}
    </div>
  );
}

/**
 * An option as the kit's radio card: the label with the agent's pick tagged, then what it does next. On the phone's
 * screen (`screen`) it has 14 px of padding, a 15 px title and a 14 px description.
 */
function OptionCard({
  option,
  name,
  checked,
  onPick,
  screen = false,
}: {
  option: DecisionOption;
  name: string;
  checked: boolean;
  onPick: () => void;
  screen?: boolean;
}) {
  const id = useId();
  return (
    <label
      htmlFor={id}
      className={cn(
        "grid cursor-pointer grid-cols-[16px_minmax(0,1fr)] gap-3 rounded-sm border border-border-strong bg-card transition-colors hover:bg-accent max-md:min-h-11",
        "has-checked:border-brand has-checked:bg-surface-selected",
        screen ? "p-3.5" : "p-3",
      )}
      data-testid={`decision-choice-${option.key}`}
      data-key={option.key}
      data-checked={checked || undefined}
    >
      <input
        id={id}
        type="radio"
        name={name}
        value={option.key}
        checked={checked}
        onChange={onPick}
        className="mt-0.5 size-4 cursor-pointer accent-brand"
        aria-labelledby={`${id}-title`}
        aria-describedby={option.description ? `${id}-description` : undefined}
      />
      <OptionText option={option} id={id} screen={screen} />
    </label>
  );
}

function OptionText({ option, id, screen = false }: { option: DecisionOption; id: string; screen?: boolean }) {
  return (
    <span className="flex min-w-0 flex-col gap-0.5">
      <span
        id={`${id}-title`}
        className={cn("flex flex-wrap items-center gap-2 font-medium text-foreground [overflow-wrap:anywhere]", screen ? "text-[15px] leading-[22px]" : "text-sm")}
      >
        {option.label}
        {/* A space for the accessible name ("Deploy now Agent's pick"); a flex container does not draw it. */}
        {option.recommended ? <> <AgentPickTag /></> : null}
      </span>
      {option.description ? (
        <span
          id={`${id}-description`}
          className={cn("whitespace-pre-line text-muted-foreground [overflow-wrap:anywhere]", screen ? "text-sm leading-5" : "text-[13px] leading-[18px]")}
        >
          {option.description}
        </span>
      ) : null}
    </span>
  );
}

/** The options as they were offered, for anyone who cannot answer, the agent's pick tagged. */
export function OptionList({ decision, screen = false }: { decision: Decision; screen?: boolean }) {
  const t = useTranslations("inbox.answer");
  const ids = useId();
  return (
    <div className="flex flex-col gap-2" data-testid="decision-options">
      <p id={`${ids}-legend`} className="sr-only">
        {t("legend")}
      </p>
      <ul className="flex flex-col gap-2" aria-labelledby={`${ids}-legend`}>
        {decision.options.map((option, index) => (
          <li key={option.key} className={cn("rounded-sm border bg-card", screen ? "p-3.5" : "p-3")} data-testid="decision-option" data-key={option.key}>
            <OptionText option={option} id={`${ids}-${index}`} screen={screen} />
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Who answered is in the head; here the option chosen, their words, whether the agent got it, and the run that took it. */
export function AnswerSummary({ decision }: { decision: Decision }) {
  const t = useTranslations("inbox.decision");
  const option = chosenOption(decision);
  const resumed = decision.answer_run_id !== null && decision.answer_run_id !== decision.run_id ? decision.answer_run_id : null;
  return (
    <section aria-label={t("answerTitle")} className="flex flex-col gap-2" data-testid="decision-answer">
      {option ? (
        <p className="text-sm font-medium text-pretty [overflow-wrap:anywhere]" data-testid="decision-answer-option" data-key={option.key}>
          <CircleCheck className="mr-1.5 inline size-4 align-[-3px] text-success" aria-hidden="true" />
          <span className="sr-only">{t("answerOption")} </span>
          {option.label}
        </p>
      ) : null}
      {decision.answer_text ? (
        <div className="flex flex-col gap-0.5" data-testid="decision-answer-text">
          <span className="text-xs text-muted-foreground">{t("answerText")}</span>
          <Prose className="max-w-none">{decision.answer_text}</Prose>
        </div>
      ) : null}
      <p className="flex items-start gap-1.5 text-xs text-muted-foreground" data-testid="decision-delivery" data-delivered={decision.delivered_at ? "true" : "false"}>
        {decision.delivered_at ? (
          <>
            <CircleCheck className="mt-px size-3.5 shrink-0" aria-hidden="true" />
            <span>
              {t("delivered")} <Ago value={decision.delivered_at} never="-" />
            </span>
          </>
        ) : (
          <>
            <Clock className="mt-px size-3.5 shrink-0" aria-hidden="true" />
            <span>{t("notDelivered")}</span>
          </>
        )}
      </p>
      {resumed !== null ? (
        <p className="text-sm text-pretty" data-testid="decision-resumed">
          {t.rich("resumed", {
            run: decision.run_id,
            next: resumed,
            link: (chunks) => (
              <Link href={runHref(decision.project, resumed)} className={cn(LINK, "font-mono")}>
                {chunks}
              </Link>
            ),
          })}
        </p>
      ) : null}
    </section>
  );
}

/** Take over, for the owner who may open the run's terminal now: the run page's Terminal tab, or `onTakeOver` there. */
export function TakeOverButton({
  decision,
  href,
  onTakeOver,
  className,
}: {
  decision: Decision;
  href: Route;
  onTakeOver?: () => void;
  className?: string;
}) {
  const t = useTranslations("inbox.answer");
  const label = t("takeOverLabel", { run: decision.run_id });
  if (onTakeOver) {
    return (
      <Button type="button" variant="outline" onClick={onTakeOver} aria-label={label} className={className} data-testid="decision-takeover">
        <Terminal aria-hidden="true" />
        {t("takeOver")}
      </Button>
    );
  }
  return (
    <Button asChild variant="outline" className={className}>
      <Link href={href} aria-label={label} data-testid="decision-takeover">
        <Terminal aria-hidden="true" />
        {t("takeOver")}
      </Link>
    </Button>
  );
}

/**
 * The owner's answer, as state a layout renders: the options with the agent's pick chosen, so one click on Send answer
 * (or Cmd or Ctrl with Enter anywhere in the form) answers with it; Clear the choice leaves words alone as the answer.
 * Nothing chosen or written, or words over 4 KiB of UTF-8, is refused beside the field. What the hub says is a toast:
 * the answer sent, with a link to the run that takes it, or why the hub refused it, which stays until dismissed; after a
 * 409 the decision is shown as the hub holds it. The card (`AnswerForm`) and the phone's screen lay it out.
 */
export function useAnswerForm(
  decision: Decision,
  { onAnswered, onConflict }: { onAnswered?: (decision: Decision) => void; onConflict?: (decision: Decision) => void } = {},
) {
  const t = useTranslations("inbox.answer");
  const ids = useId();
  const failure = useAnswerFailure();
  const terminalHref = useDecisionTerminalHref(decision);
  const answer = useAnswerDecision(decision, {
    onAnswered: (answered) => {
      const next = answered.answer_run_id !== null && answered.answer_run_id !== answered.run_id ? answered.answer_run_id : null;
      notify(
        next !== null
          ? {
              tone: "success",
              text: t("sentResumedTitle"),
              description: t("sentResumed", { run: answered.run_id, next }),
              link: { label: t("openRun", { id: next }), href: runHref(answered.project, next) },
            }
          : {
              tone: "success",
              text: t("sentTitle", { run: answered.run_id }),
              description: t("sentText"),
              link: { label: t("openRun", { id: answered.run_id }), href: runHref(answered.project, answered.run_id) },
            },
      );
      onAnswered?.(answered);
    },
    onFailed: (error) => notifyFailure(t("failed"), failure(error, decision)),
    onConflict,
  });
  const [option, setOption] = useState<string | null>(() => initialOption(decision));
  const [text, setText] = useState("");
  const [problem, setProblem] = useState<AnswerProblem>(null);
  const bytes = utf8Bytes(text.trim());

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (answer.isPending) return;
    const found = answerProblem(option, text);
    setProblem(found);
    // Focus goes where the answer needs work: the first option, or the field (`AnswerFields` gives them these ids).
    if (found === "empty") return document.getElementById(`${ids}-options`)?.querySelector<HTMLInputElement>("input")?.focus();
    if (found === "long") return document.getElementById(`${ids}-text`)?.focus();
    answer.mutate(answerBody(option, text));
  };

  const onKeyDown = (event: KeyboardEvent<HTMLFormElement>) => {
    if (!isSendShortcut(event.nativeEvent)) return;
    event.preventDefault();
    event.currentTarget.requestSubmit();
  };

  return {
    decision,
    ids,
    option,
    pick: (key: string | null) => {
      setOption(key);
      if (key !== null) setProblem(null);
    },
    text,
    write: (value: string) => {
      setText(value);
      setProblem(null);
    },
    problem,
    bytes,
    long: bytes > MAX_ANSWER_BYTES,
    parked: decision.run_state === "parked",
    pending: answer.isPending,
    terminalHref,
    /** The form's own props: what it submits, Cmd or Ctrl with Enter, and its name. */
    formProps: {
      onSubmit: submit,
      onKeyDown,
      noValidate: true,
      "aria-label": t("title", { id: decision.id }),
      "aria-busy": answer.isPending || undefined,
      "data-testid": "decision-form",
    },
  };
}

export type AnswerFormState = ReturnType<typeof useAnswerForm>;

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
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-text`} className={cn("font-medium text-muted-foreground", screen ? "text-sm" : "text-xs")}>
          {option !== null ? t("note") : t("text")}
        </Label>
        <Textarea
          id={`${ids}-text`}
          value={form.text}
          rows={3}
          placeholder={option !== null ? t("notePlaceholder") : t("textPlaceholder")}
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
          {problem === "empty" ? t("empty") : t("long", { max: format.number(MAX_ANSWER_BYTES) })}
        </p>
      ) : null}
    </div>
  );
}

/** "The agent continues as soon as you answer", or that the parked run resumes. */
export function AnswerHint({ form, className }: { form: AnswerFormState; className?: string }) {
  const t = useTranslations("inbox.answer");
  return (
    <p className={cn("text-xs text-pretty text-fg-subtle", className)} data-testid="decision-form-hint">
      {form.parked ? t("footParked") : t("foot")}
    </p>
  );
}

/**
 * The card's answer form: the fields, then a footer with the hint, Take over when the owner may open the run's terminal,
 * and Send answer with its key (Cmd or Ctrl with Enter; from 768 px, where a keyboard is likely). `flush` keeps the
 * footer in view at the foot of a sheet that is the card.
 */
function AnswerForm({
  decision,
  flush,
  onAnswered,
  onConflict,
  onTakeOver,
}: {
  decision: Decision;
  flush: boolean;
  onAnswered?: (decision: Decision) => void;
  onConflict?: (decision: Decision) => void;
  onTakeOver?: () => void;
}) {
  const t = useTranslations("inbox.answer");
  const modifier = useModifierKey();
  const form = useAnswerForm(decision, { onAnswered, onConflict });
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
        {form.terminalHref ? <TakeOverButton decision={decision} href={form.terminalHref} onTakeOver={onTakeOver} /> : null}
        <Button type="submit" busy={form.pending} aria-keyshortcuts="Meta+Enter Control+Enter" data-testid="decision-send">
          {form.pending ? t("sending") : t("send")}
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

/** Why the visitor sees no form: someone else's decision, or one whose run can no longer take an answer. */
export function NoFormNote({ decision, access }: { decision: Decision; access: "notOwner" | "runGone" }) {
  const t = useTranslations("inbox.decision");
  const tState = useStatusText("run");
  return (
    <p className="flex items-start gap-2 rounded-sm bg-surface-sunken px-3 py-2.5 text-[13px] leading-[18px] text-pretty text-muted-foreground" data-testid="decision-locked" data-reason={access}>
      <Lock className="mt-px size-4 shrink-0" aria-hidden="true" />
      <span>
        {access === "notOwner"
          ? t("onlyOwner", { owner: decision.owner, run: decision.run_id })
          : t("runGone", { run: decision.run_id, state: tState(decision.run_state) })}
      </span>
    </p>
  );
}

export function ClosedNote({ decision }: { decision: Decision }) {
  const t = useTranslations("inbox.decision");
  if (decision.state !== "expired" && decision.state !== "cancelled") return null;
  return (
    <p className="flex items-start gap-2 text-[13px] leading-[18px] text-pretty text-muted-foreground" data-testid="decision-closed">
      <Lock className="mt-px size-4 shrink-0" aria-hidden="true" />
      <span>{decision.state === "expired" ? t("expired", { run: decision.run_id }) : t("cancelled", { run: decision.run_id })}</span>
    </p>
  );
}

/**
 * One decision as the kit's DecisionCard. While it is open: the head on `attention-soft`, the question, the agent's
 * context folded after four lines, then for the run's owner the answer form, and for anyone else the options and why
 * they cannot answer. Once answered: one compact block, the head sunken with who answered and when, the question quiet
 * and the answer under it; expired or cancelled, the same with what happened.
 *
 * `where` inbox (the sheet of the Inbox and of Home) names the run, project, plan and step in the head and makes the
 * question an h2; `where` run (a plan run's own page) names only the step and makes it an h3 under the page's section.
 * `flush` drops the card's own edge, for a sheet that is the card, and keeps the form's footer in view at its foot.
 */
export function DecisionCard({
  decision,
  where,
  headingRef,
  flush = false,
  parksAt: knownParksAt,
  onAnswered,
  onConflict,
  onTakeOver,
}: {
  decision: Decision;
  where: "inbox" | "run";
  headingRef?: Ref<HTMLHeadingElement>;
  flush?: boolean;
  /** When the run parks, if the caller holds it already (the Home's overview); read from the overview otherwise. */
  parksAt?: string | null;
  onAnswered?: (decision: Decision) => void;
  onConflict?: (decision: Decision) => void;
  /** On the run's own page: show its Terminal tab, rather than going to the page. */
  onTakeOver?: () => void;
}) {
  const ids = useId();
  const { login } = useInboxViewer();
  const access = answerAccess(decision, login);
  const parksAt = useParksAt(decision, knownParksAt);
  const level: HeadingLevel = where === "inbox" ? 2 : 3;
  const open = decision.state === "open";

  return (
    <article
      aria-labelledby={`${ids}-question`}
      className={cn("flex min-w-0 flex-col", flush ? "" : "overflow-hidden rounded-md border bg-card shadow-raised")}
      data-testid="decision"
      data-decision-id={decision.id}
      data-state={decision.state}
    >
      <DecisionHead decision={decision} where={where} access={access} parksAt={parksAt} />
      <div className={cn("flex flex-col gap-3 px-4 pt-4", access === "answer" ? "pb-3" : "pb-4")}>
        <Question level={level} id={`${ids}-question`} headingRef={headingRef} quiet={!open}>
          {decision.question.trim()}
        </Question>
        {open && decision.context ? <FoldedContext text={decision.context} /> : null}
        {decision.state === "answered" ? <AnswerSummary decision={decision} /> : null}
        <ClosedNote decision={decision} />
        {open && access !== "answer" ? (
          <>
            <OptionList decision={decision} />
            {access === "notOwner" || access === "runGone" ? <NoFormNote decision={decision} access={access} /> : null}
          </>
        ) : null}
      </div>
      {access === "answer" ? (
        <AnswerForm key={decision.id} decision={decision} flush={flush} onAnswered={onAnswered} onConflict={onConflict} onTakeOver={onTakeOver} />
      ) : null}
    </article>
  );
}
