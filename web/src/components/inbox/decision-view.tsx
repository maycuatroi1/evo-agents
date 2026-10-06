"use client";

import { Check, CircleCheck, Clock, Eraser, Info, Loader2, Lock, Send } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, type Ref, useId, useRef, useState } from "react";

import { type Notice, NoticeArea, useNotice } from "@/components/admin/notice";
import { SafeMarkdown } from "@/components/memories/markdown";
import { Prose } from "@/components/plans/prose";
import { planHref, stepHref } from "@/components/plans/links";
import { RunStateBadge } from "@/components/runs/badges";
import { Choice, Section } from "@/components/runs/dispatch-fields";
import { runHref } from "@/components/runs/queries";
import { utf8Bytes } from "@/components/runs/run-model";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Ago } from "@/components/workers/ago";
import { cn } from "@/lib/utils";

import { DecisionCategoryBadge, DecisionStateBadge, RecommendedBadge } from "./badges";
import { useAnswerDecision, useAnswerFailure, useInboxViewer } from "./hooks";
import { answerAccess, answerBody, answerProblem, type AnswerProblem, chosenOption } from "./model";
import { type Decision, type DecisionOption, MAX_ANSWER_BYTES } from "./queries";

/** The byte count shows once an answer passes this share of the limit. */
const COUNT_FROM = 0.75;

type HeadingLevel = 2 | 3;

function Heading({ level, children, className, id, headingRef }: { level: HeadingLevel; children: ReactNode; className?: string; id?: string; headingRef?: Ref<HTMLHeadingElement> }) {
  const Tag = level === 2 ? "h2" : "h3";
  return (
    <Tag id={id} ref={headingRef} tabIndex={headingRef ? -1 : undefined} className={cn("outline-none", className)}>
      {children}
    </Tag>
  );
}

function SubHeading({ level, children, id }: { level: HeadingLevel; children: ReactNode; id: string }) {
  const Tag = level === 2 ? "h3" : "h4";
  return (
    <Tag id={id} className="text-sm font-medium">
      {children}
    </Tag>
  );
}

function Fact({ label, children, testId }: { label: string; children: ReactNode; testId?: string }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 [overflow-wrap:anywhere]" data-testid={testId}>
        {children}
      </dd>
    </>
  );
}

const LINK = "text-brand underline-offset-4 hover:underline";
/** A link standing alone in the facts: at least 24 px tall, the target size WCAG 2.2 asks of it. */
const FACT_LINK = cn(LINK, "inline-flex min-h-6 items-center");

/** Where a decision comes from: its project, run, plan and step, when it was asked and of whom. */
function DecisionFacts({ decision, where }: { decision: Decision; where: "inbox" | "run" }) {
  const t = useTranslations("inbox.decision");
  return (
    <dl className="grid grid-cols-[max-content_minmax(0,1fr)] items-center gap-x-4 gap-y-1 text-sm" data-testid="decision-facts">
      {where === "inbox" ? (
        <>
          <Fact label={t("project")}>
            <span className="font-mono">{decision.project}</span>
          </Fact>
          <Fact label={t("run")} testId="decision-run">
            <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <Link href={runHref(decision.project, decision.run_id)} className={cn(FACT_LINK, "font-mono tabular-nums")} data-testid="decision-run-link">
                {t("runLink", { id: decision.run_id })}
              </Link>
              <RunStateBadge state={decision.run_state} />
            </span>
          </Fact>
        </>
      ) : null}
      <Fact label={t("plan")}>
        <Link href={planHref(decision.project, decision.plan_id)} className={cn(FACT_LINK, "font-mono")} data-testid="decision-plan-link">
          {decision.plan_id}
        </Link>
      </Fact>
      {decision.step_key !== null ? (
        <Fact label={t("step")}>
          <Link href={stepHref(decision.project, decision.plan_id, decision.step_key)} className={FACT_LINK} data-testid="decision-step-link">
            {t("stepLink", { key: decision.step_key })}
          </Link>
        </Fact>
      ) : null}
      <Fact label={t("asked")}>
        <Ago value={decision.asked_at} never="-" />
      </Fact>
      <Fact label={t("owner")}>
        <span className="font-mono">{decision.owner}</span>
      </Fact>
    </dl>
  );
}

/** The options as they were offered, for anyone who cannot answer: the recommended one and the one chosen marked. */
function OptionList({ decision, level }: { decision: Decision; level: HeadingLevel }) {
  const t = useTranslations("inbox.decision");
  const ids = useId();
  return (
    <section aria-labelledby={`${ids}-options`} className="flex flex-col gap-2" data-testid="decision-options">
      <SubHeading level={level} id={`${ids}-options`}>
        {t("options")}
      </SubHeading>
      <ul className="flex flex-col gap-2">
        {decision.options.map((option) => {
          const chosen = decision.answer_option === option.key;
          return (
            <li
              key={option.key}
              className={cn("flex min-w-0 items-start gap-2.5 rounded-md border px-3 py-2", chosen ? "border-success/30 bg-success-soft/60" : "bg-card")}
              data-testid="decision-option"
              data-key={option.key}
              data-chosen={chosen || undefined}
            >
              {chosen ? <CircleCheck className="mt-0.5 size-4 shrink-0 text-success" aria-hidden="true" /> : <span className="mt-0.5 size-4 shrink-0" aria-hidden="true" />}
              <OptionText option={option} />
              <span className="flex shrink-0 flex-col items-end gap-1">
                {option.recommended ? <RecommendedBadge /> : null}
                {chosen ? (
                  <Badge variant="success" data-testid="decision-chosen">
                    <Check aria-hidden="true" />
                    {t("chosen")}
                  </Badge>
                ) : null}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function OptionText({ option }: { option: DecisionOption }) {
  return (
    <span className="flex min-w-0 flex-1 flex-col gap-0.5">
      <span className="text-sm [overflow-wrap:anywhere]">
        {option.label} <span className="font-mono text-xs text-muted-foreground">({option.key})</span>
      </span>
      {option.description ? <span className="text-xs leading-snug whitespace-pre-line text-muted-foreground [overflow-wrap:anywhere]">{option.description}</span> : null}
    </span>
  );
}

/** Who answered and when, the words they added, whether the agent got it, and the run that took it. */
function AnswerSummary({ decision, level }: { decision: Decision; level: HeadingLevel }) {
  const t = useTranslations("inbox.decision");
  const ids = useId();
  const option = chosenOption(decision);
  const resumed = decision.answer_run_id !== null && decision.answer_run_id !== decision.run_id ? decision.answer_run_id : null;
  return (
    <section
      aria-labelledby={`${ids}-answer`}
      className="flex flex-col gap-2 rounded-md border border-success/20 bg-success-soft/40 px-3 py-2.5"
      data-testid="decision-answer"
    >
      <SubHeading level={level} id={`${ids}-answer`}>
        {t("answerTitle")}
      </SubHeading>
      <p className="text-sm text-pretty" data-testid="decision-answered-by">
        {t.rich("answeredBy", {
          login: decision.answered_by ?? decision.owner,
          who: (chunks) => <span className="font-mono">{chunks}</span>,
        })}{" "}
        {decision.answered_at ? <Ago value={decision.answered_at} never="-" /> : null}
      </p>
      {option ? (
        <p className="text-sm text-pretty [overflow-wrap:anywhere]" data-testid="decision-answer-option">
          <span className="text-muted-foreground">{t("answerOption")}</span> {option.label}{" "}
          <span className="font-mono text-xs text-muted-foreground">({option.key})</span>
        </p>
      ) : null}
      {decision.answer_text ? (
        <div className="flex flex-col gap-1" data-testid="decision-answer-text">
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

/**
 * The owner's answer: one of the options as a radio card (the recommended one badged, none picked for them), words of
 * their own, or both, then Send answer. Nothing chosen or written, or words over 4 KiB of UTF-8, is refused here with
 * the reason next to the field; what the hub says goes to the decision's notice area through `onNotice`.
 */
export function AnswerForm({ decision, onNotice, onAnswered }: { decision: Decision; onNotice: (notice: Notice) => void; onAnswered?: (decision: Decision) => void }) {
  const t = useTranslations("inbox.answer");
  const format = useFormatter();
  const ids = useId();
  const failure = useAnswerFailure();
  const answer = useAnswerDecision(decision, {
    onAnswered: (answered) => {
      const next = answered.answer_run_id !== null && answered.answer_run_id !== answered.run_id ? answered.answer_run_id : null;
      onNotice({
        tone: "success",
        text: next !== null ? t("sentResumed", { run: answered.run_id, next }) : t("sent", { run: answered.run_id }),
      });
      onAnswered?.(answered);
    },
    onFailed: (error) => onNotice({ tone: "error", ...failure(error, decision) }),
  });
  const [option, setOption] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [problem, setProblem] = useState<AnswerProblem>(null);
  const options = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const bytes = utf8Bytes(text.trim());
  const long = bytes > MAX_ANSWER_BYTES;
  const parked = decision.run_state === "parked";

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (answer.isPending) return;
    const found = answerProblem(option, text);
    setProblem(found);
    if (found === "empty") return options.current?.querySelector<HTMLInputElement>("input")?.focus();
    if (found === "long") return box.current?.focus();
    answer.mutate(answerBody(option, text));
  };

  const problemId = `${ids}-problem`;
  return (
    <form
      onSubmit={submit}
      noValidate
      className="flex flex-col gap-4"
      aria-label={t("title", { id: decision.id })}
      aria-busy={answer.isPending || undefined}
      data-testid="decision-form"
    >
      <div ref={options}>
        <Section legend={t("options")} hint={t("optionsHint")} testId="decision-form-options">
          {decision.options.map((item) => (
            <Choice
              key={item.key}
              type="radio"
              name={`decision-${decision.id}-option`}
              value={item.key}
              checked={option === item.key}
              onChange={() => {
                setOption(item.key);
                setProblem(null);
              }}
              title={
                <>
                  {item.label} <span className="font-mono text-xs text-muted-foreground">({item.key})</span>
                </>
              }
              hint={item.description ?? undefined}
              badge={item.recommended ? <RecommendedBadge className="mt-0.5" /> : undefined}
              testId={`decision-choice-${item.key}`}
            />
          ))}
          {option !== null ? (
            <Button type="button" variant="ghost" size="sm" className="self-start" onClick={() => setOption(null)} data-testid="decision-clear-option">
              <Eraser aria-hidden="true" />
              {t("clearOption")}
            </Button>
          ) : null}
        </Section>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-text`}>{t("text")}</Label>
        <p id={`${ids}-text-hint`} className="text-xs text-pretty text-muted-foreground">
          {t("textHint")}
        </p>
        <Textarea
          ref={box}
          id={`${ids}-text`}
          value={text}
          rows={3}
          placeholder={t("placeholder")}
          onChange={(event) => {
            setText(event.target.value);
            setProblem(null);
          }}
          className="max-h-64 min-h-20 resize-y"
          aria-invalid={problem === "long" || undefined}
          aria-describedby={[`${ids}-text-hint`, problem ? problemId : null].filter(Boolean).join(" ")}
          data-testid="decision-text"
        />
        {bytes >= MAX_ANSWER_BYTES * COUNT_FROM ? (
          <span className={cn("self-end text-xs tabular-nums text-muted-foreground", long && "font-medium text-danger")} data-testid="decision-bytes">
            {t("bytes", { count: format.number(bytes), max: format.number(MAX_ANSWER_BYTES) })}
          </span>
        ) : null}
      </div>
      {problem ? (
        <p id={problemId} className="text-sm font-medium text-danger" role="alert" data-testid="decision-problem">
          {problem === "empty" ? t("empty") : t("long", { max: format.number(MAX_ANSWER_BYTES) })}
        </p>
      ) : null}
      <div className="flex flex-col gap-3 border-t pt-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="flex items-start gap-1.5 text-xs text-pretty text-muted-foreground" data-testid="decision-form-hint">
          <Info className="mt-px size-3.5 shrink-0" aria-hidden="true" />
          <span>{parked ? t("hintParked", { run: decision.run_id }) : t("hint", { run: decision.run_id })}</span>
        </p>
        <Button type="submit" size="lg" className="shrink-0" aria-disabled={answer.isPending || undefined} data-testid="decision-send">
          {answer.isPending ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <Send aria-hidden="true" />}
          {answer.isPending ? t("sending") : t("send")}
        </Button>
      </div>
    </form>
  );
}

/** Why the visitor sees no form: someone else's decision, one that is closed, or one whose run can no longer take it. */
function NoFormNote({ decision, access }: { decision: Decision; access: "notOwner" | "runGone" }) {
  const t = useTranslations("inbox.decision");
  const tState = useTranslations("runs.state");
  return (
    <p className="flex items-start gap-2 rounded-md border border-dashed px-3 py-2.5 text-sm text-pretty text-muted-foreground" data-testid="decision-locked" data-reason={access}>
      <Lock className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span>
        {access === "notOwner"
          ? t("onlyOwner", { owner: decision.owner, run: decision.run_id })
          : t("runGone", { run: decision.run_id, state: tState(decision.run_state) })}
      </span>
    </p>
  );
}

function ClosedNote({ decision }: { decision: Decision }) {
  const t = useTranslations("inbox.decision");
  if (decision.state !== "expired" && decision.state !== "cancelled") return null;
  return (
    <p className="flex items-start gap-2 rounded-md border border-dashed px-3 py-2.5 text-sm text-pretty text-muted-foreground" data-testid="decision-closed">
      <Lock className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span>{decision.state === "expired" ? t("expired", { run: decision.run_id }) : t("cancelled", { run: decision.run_id })}</span>
    </p>
  );
}

/**
 * One decision: what the agent asks and why (its context, Markdown rendered without raw HTML, as memory bodies are),
 * where it comes from, and then the answer form for the run's owner while it is open, the answer once given, or why
 * the visitor cannot answer. The Inbox shows it beside the list (`where` inbox, its question an h2); a plan run's page
 * shows its open decisions in the banner above the log (`where` run, an h3, without the project and run it is on).
 */
export function DecisionView({
  decision,
  where,
  headingRef,
  onAnswered,
  actions,
}: {
  decision: Decision;
  where: "inbox" | "run";
  headingRef?: Ref<HTMLHeadingElement>;
  onAnswered?: (decision: Decision) => void;
  /** Beside the decision's number: the Inbox's close button, the run page's link to the Inbox. */
  actions?: ReactNode;
}) {
  const t = useTranslations("inbox.decision");
  const ids = useId();
  const { login } = useInboxViewer();
  const { notice, show, clear } = useNotice();
  const access = answerAccess(decision, login);
  const level: HeadingLevel = where === "inbox" ? 2 : 3;

  return (
    <article
      aria-labelledby={`${ids}-question`}
      className="flex min-w-0 flex-col gap-4"
      data-testid="decision"
      data-decision-id={decision.id}
      data-state={decision.state}
    >
      <header className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            <span className="font-medium tracking-wide uppercase">{t("number", { id: decision.id })}</span>
            <DecisionStateBadge state={decision.state} />
            <DecisionCategoryBadge category={decision.category} />
          </p>
          {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
        </div>
        <Heading
          level={level}
          id={`${ids}-question`}
          headingRef={headingRef}
          className={cn(
            "font-semibold tracking-tight whitespace-pre-line text-pretty [overflow-wrap:anywhere]",
            level === 2 ? "text-lg" : "text-base",
          )}
        >
          {decision.question.trim()}
        </Heading>
      </header>
      <DecisionFacts decision={decision} where={where} />
      {decision.context ? (
        <section aria-labelledby={`${ids}-context`} className="flex flex-col gap-2" data-testid="decision-context">
          <SubHeading level={level} id={`${ids}-context`}>
            {t("context")}
          </SubHeading>
          <div className="rounded-md border bg-muted/30 px-3 py-2.5">
            <SafeMarkdown testId="decision-context-markdown">{decision.context}</SafeMarkdown>
          </div>
        </section>
      ) : null}
      {/* Next to the form and the answer, where the visitor's eyes are when the hub answers. */}
      <NoticeArea notice={notice} onDismiss={clear} emptyClassName="empty:-mt-4" />
      {decision.state === "answered" ? <AnswerSummary decision={decision} level={level} /> : null}
      <ClosedNote decision={decision} />
      {access === "answer" ? (
        <AnswerForm key={decision.id} decision={decision} onNotice={show} onAnswered={onAnswered} />
      ) : (
        <>
          {access === "notOwner" || access === "runGone" ? <NoFormNote decision={decision} access={access} /> : null}
          <OptionList decision={decision} level={level} />
        </>
      )}
    </article>
  );
}
