"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, CheckCheck, Info } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useContext, useState } from "react";

import { AgentAvatar } from "@/components/data/agent-avatar";
import { useMoney } from "@/components/data/facts";
import { NAME_LINK } from "@/components/data/identifier";
import { InlineError, type WriteFailure } from "@/components/feedback/notice";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { chatKeys } from "@/lib/chat-queries";
import type { Chat, GroupMessage } from "@/lib/chats";
import { chatMentionMarks, plainMentions } from "@/lib/mention-marks";
import { markHandled, type WorkCard, type WorkDecision, workKeys } from "@/lib/work";

import { ChatBackContext } from "./chat-header";
import { useChatFailure } from "./hooks";
import { preview } from "./line-model";
import type { TeamRow } from "./task-model";
import { AskStatus } from "./work-ask";
import { Decisions, Figures } from "./work-card";
import { WorkStatusPill } from "./work-look";

/** Where the source line leads: the message that opened the task, marked in the direct chat it was asked in. */
export function sourceHref(card: Pick<WorkCard, "chat_id" | "message_id">): Route {
  return `/chats/${card.chat_id}?message=${card.message_id}` as Route;
}

/**
 * The head of a task on Home (web/DESIGN.md, Chat home, The task), in place of a chat's head bar: a source line (the
 * system, when it started, a link to the message in the direct chat that opened it, and Details, which opens the
 * chat's members), the title as the page's heading, the state with what is left and Mark as handled while the work is
 * done short of its done-when, its time, runs, hop and cost, its goal and when it is done (Not met yet then), the
 * decisions asked of the visitor, and its team.
 */
export function TaskHead({
  card,
  chat,
  me,
  team,
  onDetails,
  answer,
}: {
  card: WorkCard;
  chat: Chat;
  me: string | null;
  team: TeamRow[];
  onDetails: () => void;
  answer?: (decision: WorkDecision) => ReactNode;
}) {
  const t = useTranslations("chatHome.task");
  const tCard = useTranslations("chats.work.card");
  const format = useFormatter();
  const back = useContext(ChatBackContext);
  const notMet = card.outcome === "not_met";
  const open = card.decisions.filter((decision) => decision.state === "open");
  return (
    <header className="flex flex-col gap-3 border-b px-3 py-3 sm:px-4" data-testid="chat-header" data-view="task" data-status={card.status} data-outcome={card.outcome ?? undefined}>
      <div className="flex min-w-0 items-center gap-2 text-[13px] leading-[18px] text-muted-foreground">
        {back ? (
          <Button type="button" variant="ghost" size="icon-sm" className="-ml-1 max-md:size-11 md:hidden" aria-label={back.label} onClick={back.onBack} data-testid="home-back">
            <ArrowLeft aria-hidden="true" />
          </Button>
        ) : null}
        <p className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-0.5" data-testid="task-source">
          <span className="font-medium text-foreground">{card.system.name ?? card.system.slug}</span>
          <span aria-hidden="true">-</span>
          <time dateTime={card.started_at}>{t("started", { at: format.dateTime(new Date(card.started_at), { dateStyle: "medium", timeStyle: "short" }) })}</time>
          <span aria-hidden="true">-</span>
          <Link href={sourceHref(card)} className={NAME_LINK} data-testid="task-source-link">
            {t("source")}
          </Link>
        </p>
        <Button type="button" variant="outline" size="sm" className="shrink-0 max-md:min-h-11 max-md:min-w-11" onClick={onDetails} data-testid="chat-details-open">
          <Info aria-hidden="true" />
          <span className="max-md:sr-only">{t("details")}</span>
        </Button>
      </div>
      <h1 id="chat-title" className="text-lg font-semibold tracking-tight [overflow-wrap:anywhere]" data-testid="chat-title">
        {card.title}
      </h1>
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2" data-testid="task-status">
        <WorkStatusPill status={card.status} />
        {card.left ? (
          <p className="min-w-0 flex-1 text-[13px] leading-[18px] [overflow-wrap:anywhere]" data-testid="task-left">
            <span className="text-muted-foreground">{t("left")} </span>
            {card.left}
          </p>
        ) : null}
        {card.handled_by ? (
          <p className="text-[13px] text-muted-foreground" data-testid="task-handled">
            {t("handledBy", { login: card.handled_by })}
          </p>
        ) : null}
        {notMet ? <MarkHandled chatId={chat.id} /> : null}
      </div>
      <Figures card={card} />
      <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-[13px] leading-[18px] sm:grid-cols-[max-content_minmax(0,1fr)]">
        <dt className="text-muted-foreground">{tCard("goal")}</dt>
        <dd className="[overflow-wrap:anywhere]" data-testid="task-goal">
          {card.goal ?? <span className="text-fg-subtle italic">{tCard("noGoal")}</span>}
        </dd>
        <dt className="text-muted-foreground max-sm:mt-1">{tCard("doneWhen")}</dt>
        <dd className="flex flex-wrap items-center gap-x-2 gap-y-1 [overflow-wrap:anywhere]" data-testid="task-done-when">
          {card.done_when ?? <span className="text-fg-subtle italic">{tCard("noDoneWhen")}</span>}
          {notMet ? (
            <span className="rounded-xs bg-attention-soft px-1.5 text-xs font-medium text-attention" data-testid="task-not-met">
              {t("notMet")}
            </span>
          ) : null}
        </dd>
      </dl>
      {open.length ? <Decisions decisions={open} me={me} answer={answer} /> : null}
      <Team rows={team} chat={chat} />
    </header>
  );
}

function MarkHandled({ chatId }: { chatId: number }) {
  const t = useTranslations("chatHome.task");
  const queryClient = useQueryClient();
  const failure = useChatFailure();
  const [problem, setProblem] = useState<WriteFailure | null>(null);
  const mark = useMutation({
    mutationFn: () => markHandled(browserApi, chatId),
    onMutate: () => setProblem(null),
    onError: (cause) => setProblem(failure(cause)),
    onSuccess: (card) => {
      queryClient.setQueryData(workKeys.card(chatId), card);
      // The rail moves the task out of Needs you once the list says the outcome is no longer not met.
      void queryClient.invalidateQueries({ queryKey: chatKeys.list });
    },
  });
  return (
    <div className="flex flex-col gap-1">
      <Button type="button" size="sm" busy={mark.isPending} onClick={() => mark.mutate()} data-testid="task-mark-handled">
        <CheckCheck aria-hidden="true" />
        {t("markHandled")}
      </Button>
      {problem ? <InlineError text={t("handleFailed")} detail={problem.detail} requestId={problem.requestId} /> : null}
    </div>
  );
}

function Team({ rows, chat }: { rows: TeamRow[]; chat: Chat }) {
  const t = useTranslations("chatHome.task");
  const money = useMoney();
  if (rows.length === 0) return null;
  const result = (message: GroupMessage | null) => (message ? plainMentions(preview(message.body), chatMentionMarks(chat)) : null);
  return (
    <section aria-labelledby="task-team-title" className="flex flex-col gap-1.5">
      <h2 id="task-team-title" className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
        {t("team")}
      </h2>
      <ul className="flex flex-col gap-2" data-testid="task-team">
        {rows.map((row) => {
          const known = chat.agents.find((agent) => agent.handle === row.agent);
          return (
            <li key={row.agent} className="flex min-w-0 items-start gap-2" data-testid="task-team-member" data-agent={row.agent} data-coordinator={row.coordinator || undefined}>
              <AgentAvatar agent={{ name: known?.name ?? row.agent, handle: row.agent, color: known?.color ?? "#64748b", icon_url: known?.icon_url ?? null }} size="sm" />
              <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[13px] leading-[18px]">
                  <span className="font-mono">@{row.agent}</span>
                  {row.coordinator ? <span className="text-xs text-muted-foreground">{t("coordinator")}</span> : null}
                  {row.work ? <WorkStatusPill status={row.work} testId="task-team-status" /> : row.status ? <AskStatus status={row.status} /> : null}
                  <span className="text-xs text-muted-foreground tabular-nums">{t("teamFigures", { runs: row.runs, cost: money(row.usd) })}</span>
                </div>
                <p className="truncate text-[13px] leading-[18px] text-muted-foreground" data-testid="task-team-result">
                  {result(row.result) ?? <span className="text-fg-subtle italic">{t("noResult")}</span>}
                </p>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
