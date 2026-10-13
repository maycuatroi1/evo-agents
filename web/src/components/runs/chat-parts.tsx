"use client";

import { Bot, CircleCheck, MessageSquare } from "lucide-react";
import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";

import type { ChatStatus, RunChat } from "./queries";

const STATUS_LOOK: Record<ChatStatus, string> = {
  working: "border-running/30 bg-running-soft text-running",
  waiting: "border-attention/30 bg-attention-soft text-attention",
  ended: "border-border bg-muted text-muted-foreground",
};

/**
 * Whose turn it is in an author run's chat: the agent works, it waits for the owner (and how many of its decisions
 * still wait for an answer), or the chat ended; `ending` once End chat was asked and the agent's turn is not over.
 */
export function ChatStatusPill({ chat, owner, ending, open }: { chat: RunChat; owner: boolean; ending: boolean; open: number }) {
  const t = useTranslations("runs.author.chat");
  const who = { login: chat.owner, you: owner ? "yes" : "no" };
  return (
    <span
      className={cn("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium", STATUS_LOOK[chat.status])}
      data-testid="run-chat-status"
      data-status={chat.status}
      data-open-decisions={open}
    >
      {chat.status === "working" ? (
        <span className="relative inline-flex size-2 rounded-full bg-running">
          <span className="absolute inset-0 animate-live-ping rounded-full bg-running" aria-hidden="true" />
        </span>
      ) : chat.status === "waiting" ? (
        <MessageSquare className="size-3.5" aria-hidden="true" />
      ) : (
        <CircleCheck className="size-3.5" aria-hidden="true" />
      )}
      {ending
        ? t("status.ending")
        : chat.status === "waiting" && open > 0
          ? t("status.waitingDecisions", { ...who, count: open })
          : t(`status.${chat.status}`, who)}
    </span>
  );
}

/** The agent at work in its turn: three dots that pulse, read out as "typing". */
export function Typing() {
  const t = useTranslations("runs.author.chat");
  return (
    <div className="flex items-center gap-3" data-testid="run-chat-typing">
      <span className="grid size-7 shrink-0 place-items-center rounded-full border border-running bg-card text-running" aria-hidden="true">
        <Bot className="size-4" />
      </span>
      <span className="flex items-center gap-[3px]">
        <span className="sr-only">{t("typing")}</span>
        {[0, 1, 2].map((dot) => (
          <i key={dot} aria-hidden="true" className="size-[5px] animate-typing rounded-full bg-running" style={{ animationDelay: `${dot * 0.2}s` }} />
        ))}
      </span>
    </div>
  );
}

/** The Trace's own dots while the agent is at work, under its last row. */
export function TraceTyping() {
  const t = useTranslations("runs.detail.trace");
  return (
    <div className="grid grid-cols-[28px_minmax(0,1fr)] gap-x-3 pt-2 pb-1" data-testid="trace-typing">
      <span className="grid size-7 place-items-center rounded-sm border border-running bg-card text-running" aria-hidden="true">
        <Bot className="size-4" />
      </span>
      <span className="flex items-center gap-[3px]">
        <span className="sr-only">{t("working")}</span>
        {[0, 1, 2].map((dot) => (
          <i key={dot} aria-hidden="true" className="size-[5px] animate-typing rounded-full bg-running" style={{ animationDelay: `${dot * 0.2}s` }} />
        ))}
      </span>
    </div>
  );
}
