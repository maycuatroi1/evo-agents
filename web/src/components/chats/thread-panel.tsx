"use client";

import { ArrowDown, Maximize2, MessageSquareReply, Minimize2, RotateCcw, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { type CSSProperties, useEffect, useState } from "react";

import { FileDropFrame } from "@/components/data/attachments";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { Chat, GroupMessage } from "@/lib/chats";
import { cn } from "@/lib/utils";

import { ChatComposer } from "./composer";
import { type Feed, replyCount, threadMessages } from "./feed-model";
import { runsOn } from "./line-model";
import { type MessageActions, MessageItem } from "./message-item";
import { useFollow } from "./use-follow";

type Props = Omit<MessageActions, "onThread"> & {
  chat: Chat;
  me: string | null;
  feed: Feed;
  root: number;
  /** The message to show in view and marked, from a link to it. */
  highlight: number | null;
  loadThread: (root: number) => Promise<void>;
  onSent: (message: GroupMessage) => void;
  onClose: () => void;
  /** Whether the thread takes the whole chat view, and the switch between that and its side place; none, no switch. */
  expanded?: boolean;
  onExpand?: (expanded: boolean) => void;
  /** The ended chat takes no message: the composer is left out. */
  readOnly: boolean;
  className?: string;
  style?: CSSProperties;
};

/**
 * A thread (web/DESIGN.md, Chats): the message that starts it, its replies, and a composer that answers in it. From
 * xl a column beside the chat; below xl the same panel in a sheet. It reads the whole thread once it opens (the chat's
 * page holds only its latest messages), and the chat's stream keeps it current after that.
 */
export function ThreadPanel({ chat, me, feed, root, highlight, loadThread, onSent, onClose, expanded = false, onExpand, readOnly, onReact, reacting, under, className, style }: Props) {
  const t = useTranslations("chats.thread");
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const [attempt, setAttempt] = useState(0);
  const messages = threadMessages(feed, root);
  const first = messages[0]?.id === root ? messages[0] : null;
  const replies = messages.filter((message) => message.id !== root);
  const count = replyCount(feed, root);
  const { region, behind, toEnd } = useFollow(messages.length, null, feed.lastSeq);

  useEffect(() => {
    let current = true;
    loadThread(root).then(
      () => current && setState("ready"),
      () => current && setState("failed"),
    );
    return () => {
      current = false;
    };
  }, [root, loadThread, attempt]);

  useEffect(() => {
    if (state !== "ready" || highlight === null) return;
    document.getElementById(`thread-message-${highlight}`)?.scrollIntoView({ block: "center" });
  }, [state, highlight]);

  const solo = chat.people.length === 1;
  return (
    <section
      aria-labelledby="chat-thread-title"
      className={cn("flex min-h-0 flex-col", className)}
      style={style}
      data-testid="chat-thread"
      data-root={root}
      data-state={state}
    >
      <FileDropFrame className="flex min-h-0 flex-1 flex-col" data-testid="chat-thread-drop">
        <header className="flex items-center gap-2 border-b px-4 py-3">
          <MessageSquareReply className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <h2 id="chat-thread-title" className="min-w-0 flex-1 truncate text-[15px] leading-[22px] font-semibold">
            {t("title")}
          </h2>
          <span className="text-xs text-fg-subtle tabular-nums" data-testid="chat-thread-count">
            {t("count", { count })}
          </span>
          {onExpand ? (
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label={t(expanded ? "collapse" : "expand")}
              title={t(expanded ? "collapse" : "expand")}
              aria-pressed={expanded}
              onClick={() => onExpand(!expanded)}
              data-testid="chat-thread-expand"
            >
              {expanded ? <Minimize2 aria-hidden="true" /> : <Maximize2 aria-hidden="true" />}
            </Button>
          ) : null}
          <Button type="button" variant="ghost" size="icon-sm" aria-label={t("close")} onClick={onClose} data-testid="chat-thread-close">
            <X aria-hidden="true" />
          </Button>
        </header>
        <div className="relative min-h-0 flex-1">
          <div
            ref={region}
            role="log"
            aria-live="polite"
            aria-label={t("log")}
            tabIndex={0}
            className="h-full overflow-y-auto overscroll-contain px-2 py-3 outline-none [overflow-anchor:none] focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset sm:px-3"
            data-testid="chat-thread-log"
          >
            {state === "failed" && !first ? (
              <div role="alert" className="flex flex-col items-start gap-2 px-2 text-sm text-danger" data-testid="chat-thread-error">
                {t("failed")}
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    setState("loading");
                    setAttempt(attempt + 1);
                  }}
                >
                  <RotateCcw aria-hidden="true" />
                  {t("retry")}
                </Button>
              </div>
            ) : !first && state === "loading" ? (
              <div role="status" className="flex flex-col gap-3 px-2" data-testid="chat-thread-loading">
                <span className="sr-only">{t("loading")}</span>
                <Skeleton className="h-16 w-full" />
                <Skeleton className="h-10 w-3/4" />
              </div>
            ) : (
              <ol className={cn("flex flex-col", expanded && "mx-auto w-full max-w-4xl")}>
                {first ? (
                  <ThreadItem message={first} chat={chat} me={me} highlight={highlight} onReact={onReact} reacting={reacting} under={under} />
                ) : null}
                <li className="flex items-center gap-3 px-3 pt-4 pb-1 text-xs text-fg-subtle" data-testid="chat-thread-divider">
                  <span>{t("replies", { count })}</span>
                  <span aria-hidden="true" className="h-px flex-1 bg-border" />
                </li>
                {replies.map((message, index) => (
                  <ThreadItem
                    key={message.id}
                    message={message}
                    runOn={runsOn(replies[index - 1], message, true)}
                    chat={chat}
                    me={me}
                    highlight={highlight}
                    onReact={onReact}
                    reacting={reacting}
                    under={under}
                  />
                ))}
                {state === "ready" && !replies.length ? (
                  <li className="px-3 py-2 text-sm text-muted-foreground" data-testid="chat-thread-empty">
                    {t("empty")}
                  </li>
                ) : null}
              </ol>
            )}
          </div>
          {behind ? (
            <Button type="button" variant="outline" size="sm" className="absolute bottom-3 left-1/2 -translate-x-1/2 shadow-popover" onClick={toEnd}>
              <ArrowDown aria-hidden="true" />
              {t("latest")}
            </Button>
          ) : null}
        </div>
        {readOnly ? null : (
          <div className={cn("border-t p-3", expanded && "px-[max(0.75rem,calc((100%-56rem)/2))]")}>
            <ChatComposer
              chat={chat}
              me={me}
              replyTo={root}
              onSent={onSent}
              label={t("composerLabel")}
              placeholder={t("placeholder")}
              hint={chat.kind === "direct" ? t("directHint", { name: chat.agents[0]?.name ?? chat.title }) : solo ? t("soloHint") : t("hint")}
              testId="thread-composer"
            />
          </div>
        )}
      </FileDropFrame>
    </section>
  );
}

function ThreadItem({
  message,
  runOn = false,
  chat,
  me,
  highlight,
  onReact,
  reacting,
  under,
}: Pick<Props, "chat" | "me" | "highlight" | "onReact" | "reacting" | "under"> & { message: GroupMessage; runOn?: boolean }) {
  return (
    <MessageItem
      message={message}
      runOn={runOn}
      chat={chat}
      me={me}
      highlighted={highlight === message.id}
      onReact={onReact}
      reacting={reacting}
      under={under}
      domId={`thread-message-${message.id}`}
    />
  );
}
