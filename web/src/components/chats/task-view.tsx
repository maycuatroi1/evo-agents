"use client";

import { ChevronDown, History, ScrollText } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import type { GroupMessage } from "@/lib/chats";
import { chatMentionMarks, plainMentions } from "@/lib/mention-marks";
import type { WorkCard, WorkDecision } from "@/lib/work";
import { cn } from "@/lib/utils";

import type { ChatLineProps } from "./chat-view";
import { MessageItem, READING_COLUMN } from "./message-item";
import { TaskHead } from "./task-head";
import { BRIEF_LINES, type Delegation, type FoldedItem, foldLogs, foldRepeats, taskLine, team } from "./task-model";
import { AskStatus } from "./work-ask";

type Shared = Pick<ChatLineProps, "chat" | "me" | "onReact" | "reacting" | "under" | "highlight">;

/**
 * A task on Home (web/DESIGN.md, Chat home, The task): its head (`TaskHead`) in place of the chat's head bar, then its
 * conversation. Each hand-off is a card (the brief to three lines with Read all, the agent's updates, the hand-offs it
 * opened inside, its result); the hub's lines that repeat a message fold into one row of notices; a pasted log longer
 * than six lines shows six until it is opened; the coordinator's last word is labelled Answer. The threads of the
 * hand-offs are read whole once the card names them.
 */
export function TaskLine({ card, answer, ...props }: ChatLineProps & { card: WorkCard; answer?: (decision: WorkDecision) => ReactNode }) {
  const t = useTranslations("chatHome.task");
  const tMessages = useTranslations("chats.messages");
  const { chat, me, feed, loadOlder, loadThread, onDetails } = props;
  const messages = useMemo(() => [...feed.entries.values()].map((entry) => entry.message), [feed.entries]);
  const line = useMemo(() => taskLine(card, chat.id, messages), [card, chat.id, messages]);
  const final = line.answer === null ? null : (messages.find((message) => message.id === line.answer) ?? null);
  const rows = useMemo(() => team(card, messages, final), [card, messages, final]);
  const [older, setOlder] = useState<"idle" | "loading" | "failed">("idle");

  // The feed's first page holds the latest messages of the chat: a hand-off's thread is read whole, once.
  const read = useRef(new Set<number>());
  const roots = card.asks.flatMap((ask) => (ask.thread && ask.thread.chat_id === chat.id ? [ask.thread.root_id] : [])).join(",");
  useEffect(() => {
    for (const root of roots ? roots.split(",").map(Number) : []) {
      if (read.current.has(root)) continue;
      read.current.add(root);
      void loadThread(root).catch(() => read.current.delete(root));
    }
  }, [roots, loadThread]);

  const earlier = async () => {
    setOlder("loading");
    try {
      await loadOlder();
      setOlder("idle");
    } catch {
      setOlder("failed");
    }
  };

  return (
    <div
      role="region"
      aria-label={t("log", { title: card.title })}
      tabIndex={0}
      className="min-h-0 flex-1 overflow-y-auto overscroll-contain outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
      data-testid="chat-log"
      data-view="task"
    >
      <TaskHead card={card} chat={chat} me={me} team={rows} onDetails={onDetails} answer={answer} />
      <section aria-labelledby="task-conversation-title" className="px-2 pt-2 pb-3 sm:px-3">
        <h2 id="task-conversation-title" className="sr-only">
          {t("conversation")}
        </h2>
        {feed.more ? (
          <div className="flex flex-col items-center gap-1.5 pt-2 pb-1">
            <Button type="button" variant="outline" size="sm" busy={older === "loading"} onClick={() => void earlier()} data-testid="chat-older">
              <History aria-hidden="true" />
              {older === "loading" ? tMessages("loadingOlder") : tMessages("older")}
            </Button>
            {older === "failed" ? (
              <p role="alert" className="text-xs text-danger" data-testid="chat-older-error">
                {tMessages("olderFailed")}
              </p>
            ) : null}
          </div>
        ) : null}
        <ol className={`flex flex-col gap-1 ${READING_COLUMN}`} data-testid="task-conversation">
          {line.items.map((item) =>
            item.kind === "delegation" ? (
              <DelegationCard key={`d-${item.delegation.root}`} node={item.delegation} {...props} />
            ) : (
              <Folded key={item.kind === "notices" ? `n-${item.key}` : item.message.id} item={item} label={item.kind === "message" && item.message.id === line.answer ? t("answer") : null} {...props} />
            ),
          )}
        </ol>
      </section>
    </div>
  );
}

/** A message of the task, or the hub's notices folded into one row that opens to them. */
function Folded({ item, label, ...props }: Shared & { item: FoldedItem; label: string | null }) {
  const t = useTranslations("chatHome.task");
  const [open, setOpen] = useState(false);
  if (item.kind === "message") return <TaskMessage message={item.message} label={label} {...props} />;
  return (
    <li className="flex flex-col gap-1 px-2 py-1 sm:px-3" data-testid="task-notices" data-count={item.messages.length}>
      <Button type="button" variant="ghost" size="sm" className="w-fit text-muted-foreground" aria-expanded={open} onClick={() => setOpen(!open)} data-testid="task-notices-toggle">
        <ChevronDown className={cn("transition-transform", open && "rotate-180")} aria-hidden="true" />
        {t("notices", { count: item.messages.length })}
      </Button>
      {open ? (
        <ol className="flex flex-col border-l pl-2">
          {item.messages.map((message) => (
            <TaskMessage key={message.id} message={message} label={null} {...props} />
          ))}
        </ol>
      ) : null}
    </li>
  );
}

/** A message, its label (Answer, Result) above it, a long pasted log cut to six lines until Show whole log. */
function TaskMessage({ message, label, chat, me, onReact, reacting, under, highlight }: Shared & { message: GroupMessage; label: string | null }) {
  const t = useTranslations("chatHome.task");
  const [whole, setWhole] = useState(false);
  const folded = useMemo(() => foldLogs(message.body), [message.body]);
  const shown = whole || folded.hidden === 0 ? message : { ...message, body: folded.body };
  return (
    <li className="flex flex-col" data-testid="task-message" data-label={label ?? undefined}>
      {label ? (
        <span className="ml-12 w-fit mt-2 rounded-xs bg-success-soft px-1.5 py-0.5 text-xs font-semibold text-success sm:ml-[3.25rem]" data-testid="task-message-label">
          {label}
        </span>
      ) : null}
      <ol>
        <MessageItem message={shown} chat={chat} me={me} highlighted={highlight === message.id} onReact={onReact} reacting={reacting} under={under} />
      </ol>
      {folded.hidden > 0 ? (
        <Button type="button" variant="ghost" size="sm" className="ml-12 w-fit sm:ml-[3.25rem]" aria-expanded={whole} onClick={() => setWhole(!whole)} data-testid="task-log-toggle">
          <ScrollText aria-hidden="true" />
          {whole ? t("hideLog") : t("showLog", { count: folded.hidden })}
        </Button>
      ) : null}
    </li>
  );
}

/** A brief longer than this many characters may run past three lines: it gets Read all. */
const LONG_BRIEF = 240;

/** A hand-off: who asked whom and how it goes, the brief, the agent's updates, the hand-offs it opened, its result. */
function DelegationCard({ node, ...props }: Shared & { node: Delegation }) {
  const t = useTranslations("chatHome.task");
  const tAsk = useTranslations("chats.work.ask");
  const [all, setAll] = useState(false);
  const first = node.asks[0];
  const last = node.asks.at(-1)!;
  // The brief is what the asking agent wrote (the question on the main line); its goal stands in until it is read.
  const brief = node.question ? plainMentions(node.question.body, chatMentionMarks(props.chat)) : (first.brief ?? "");
  const long = brief.length > LONG_BRIEF || brief.split("\n").length > BRIEF_LINES;
  const updates = foldRepeats(node.thread);
  return (
    <li className="px-2 py-1.5 sm:px-3" data-testid="task-delegation" data-agent={node.agent ?? undefined} data-status={last.status}>
      <section aria-label={node.agent ? tAsk(first.kind, { from: node.from, to: node.agent }) : tAsk("refusedTitle", { from: node.from })} className="flex min-w-0 flex-col gap-2 rounded-md border bg-card px-3 py-2.5 shadow-raised">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <h3 className="min-w-0 flex-1 text-sm font-medium [overflow-wrap:anywhere]">
            {node.agent ? tAsk(first.kind, { from: node.from, to: node.agent }) : tAsk("refusedTitle", { from: node.from })}
          </h3>
          <AskStatus status={last.status} />
        </div>
        {brief ? (
          <div className="flex flex-col items-start gap-1">
            <p className={cn("text-[13px] leading-[18px] whitespace-pre-line [overflow-wrap:anywhere]", !all && "line-clamp-3")} data-testid="task-delegation-brief">
              {brief}
            </p>
            {long ? (
              <Button type="button" variant="link" size="sm" className="h-auto px-0" aria-expanded={all} onClick={() => setAll(!all)} data-testid="task-delegation-read-all">
                {all ? t("readLess") : t("readAll")}
              </Button>
            ) : null}
          </div>
        ) : null}
        {updates.length || node.children.length ? (
          <ol className="-mx-2 flex flex-col sm:-mx-3" data-testid="task-delegation-updates">
            {updates.map((item) => (
              <Folded key={item.kind === "notices" ? `n-${item.key}` : item.message.id} item={item} label={item.kind === "message" && item.message.id === node.result?.id ? t("result") : null} {...props} />
            ))}
            {node.children.map((child) => (
              <DelegationCard key={`d-${child.root}`} node={child} {...props} />
            ))}
          </ol>
        ) : (
          <p className="text-[13px] text-fg-subtle italic">{tAsk("noAnswer")}</p>
        )}
      </section>
    </li>
  );
}
