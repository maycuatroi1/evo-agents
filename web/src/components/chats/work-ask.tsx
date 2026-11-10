"use client";

import { Ban, MessageSquareReply, ShieldAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { OtherStatusBadge, type RunStatus, STATUS_LOOKS, StatusBadge, StatusPill } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { chatMentionMarks, plainMentions } from "@/lib/mention-marks";
import type { Chat, GroupMessage } from "@/lib/chats";
import type { WorkAsk, WorkDecision } from "@/lib/work";

import { preview } from "./line-model";
import { useOpenThread } from "./open-thread";

const CARD = "flex w-full max-w-[36rem] min-w-0 flex-col gap-2 rounded-md border bg-card px-3 py-2.5 shadow-raised";

/** A sub-task's state: its run's as a run's pill, refused or failed before a run in `danger`, anything else as written. */
export function AskStatus({ status }: { status: string }) {
  const t = useTranslations("chats.work.ask");
  if (status in STATUS_LOOKS.run) return <StatusBadge kind="run" status={status as RunStatus} data-testid="work-ask-status" />;
  if (status === "refused" || status === "error") {
    return <StatusPill look={{ tone: "danger", icon: Ban }} label={t(status)} data-status={status} data-testid="work-ask-status" />;
  }
  return <OtherStatusBadge raw={status} data-testid="work-ask-status" />;
}

/**
 * The card of a sub-task on a work chat's main line, under the question that starts the thread of the agent asked
 * (web/DESIGN.md, Work card): who asked whom, its state, the goal of its brief, what the agent last answered in its
 * thread, and Open thread, which opens that thread beside the chat.
 */
export function WorkAskCard({ ask, answer, chat }: { ask: WorkAsk; answer: GroupMessage | null; chat: Pick<Chat, "agents" | "people"> }) {
  const t = useTranslations("chats.work.ask");
  const openThread = useOpenThread();
  const id = useId();
  const root = ask.thread?.root_id ?? null;
  return (
    <section aria-labelledby={`${id}-title`} className={CARD} data-testid="work-ask-card" data-agent={ask.agent ?? undefined}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <h3 id={`${id}-title`} className="min-w-0 flex-1 text-sm font-medium [overflow-wrap:anywhere]">
          {ask.agent ? t(ask.kind, { from: ask.from_agent, to: ask.agent }) : t("refusedTitle", { from: ask.from_agent })}
        </h3>
        <AskStatus status={ask.status} />
      </div>
      {ask.brief ? (
        <p className="text-[13px] leading-[18px] [overflow-wrap:anywhere]" data-testid="work-ask-brief">
          <span className="text-muted-foreground">{t("brief")} </span>
          {ask.brief}
        </p>
      ) : null}
      <p className="text-[13px] leading-[18px] [overflow-wrap:anywhere]" data-testid="work-ask-answer">
        <span className="text-muted-foreground">{t("answer")} </span>
        {answer ? plainMentions(preview(answer.body), chatMentionMarks(chat)) : <span className="text-fg-subtle italic">{ask.status === "done" ? t("inThread") : t("noAnswer")}</span>}
      </p>
      {root !== null && openThread ? (
        <Button type="button" variant="outline" size="sm" className="w-fit" onClick={() => openThread(root)} data-testid="work-ask-open">
          <MessageSquareReply aria-hidden="true" />
          {ask.agent ? t("openThreadOf", { agent: ask.agent }) : t("openThread")}
        </Button>
      ) : null}
    </section>
  );
}

/**
 * An exception the Orchestrator took up, under the hub's message that opened its thread (web/DESIGN.md, Work card):
 * the rule that opened it, and what the Orchestrator decided (answer, reassign, ask the owner) with its words, or that
 * it is still deciding.
 */
export function WorkExceptionCard({ decision }: { decision: WorkDecision }) {
  const t = useTranslations("chats.work.exception");
  const id = useId();
  const open = decision.state === "open";
  return (
    <section aria-labelledby={`${id}-title`} className={CARD} data-testid="work-exception-card" data-state={decision.state}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <ShieldAlert className="size-4 shrink-0 text-attention" aria-hidden="true" />
        <h3 id={`${id}-title`} className="min-w-0 flex-1 text-sm font-medium">
          {decision.rule ? t("title", { rule: decision.rule }) : t("titleAny")}
        </h3>
        <StatusPill
          look={open ? { tone: "attention", icon: MessageSquareReply } : { tone: "success", icon: ShieldAlert }}
          label={open ? t("deciding") : t("decided")}
          data-testid="work-exception-state"
        />
      </div>
      {decision.question ? <p className="text-[13px] leading-[18px] text-muted-foreground [overflow-wrap:anywhere]">{decision.question}</p> : null}
      {decision.outcome ? (
        <p className="text-[13px] leading-[18px] [overflow-wrap:anywhere]" data-testid="work-exception-outcome">
          <span className="text-muted-foreground">{t("outcome")} </span>
          {t(`outcomes.${decision.outcome === "reassign" || decision.outcome === "ask_owner" ? decision.outcome : "answer"}`)}
          {decision.answer ? `: ${decision.answer}` : null}
        </p>
      ) : null}
    </section>
  );
}
