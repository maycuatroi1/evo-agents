"use client";

import { ArrowUp, Sparkles, TriangleAlert } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, type KeyboardEvent, useId, useRef, useState } from "react";

import { AgentAvatar } from "@/components/data/agent-avatar";
import { InlineError, type WriteFailure } from "@/components/feedback/notice";
import { Button } from "@/components/ui/button";
import { TOUCH_QUERY, useMediaQuery } from "@/hooks/use-media-query";
import type { ChatHomeAgent } from "@/lib/chat-keys";
import { MAX_MESSAGE_BYTES, utf8Bytes } from "@/lib/chats";
import { isChatSend, isSendShortcut } from "@/lib/keyboard";
import { cn } from "@/lib/utils";

/**
 * The start of a chat with an agent, before its first message (web/DESIGN.md, Chat home): the agent's face, name and
 * what it does in the middle of the frame, the composer under them, and the agent's starters as chips under the box. A
 * starter puts its words in the box and the focus there, to send as they are or after an edit: nothing is sent behind
 * the visitor's back. Enter sends and Shift with Enter adds a line, as in the chat's own composer; on a touch screen
 * Enter adds a line and Send sends. `onSend` opens the direct chat when there is none yet and sends the message.
 */
export function ChatStart({
  agent,
  onSend,
  pending,
  error,
}: {
  agent: ChatHomeAgent;
  onSend: (body: string) => void;
  pending: boolean;
  error: WriteFailure | null;
}) {
  const t = useTranslations("chatHome.start");
  const format = useFormatter();
  const ids = useId();
  const touch = useMediaQuery(TOUCH_QUERY);
  const area = useRef<HTMLTextAreaElement>(null);
  const [text, setText] = useState("");
  const [problem, setProblem] = useState<"blank" | "long" | null>(null);

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (pending) return;
    if (!text.trim()) return setProblem("blank");
    if (utf8Bytes(text) > MAX_MESSAGE_BYTES) return setProblem("long");
    setProblem(null);
    onSend(text);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (isSendShortcut(event.nativeEvent) || isChatSend(event.nativeEvent, touch)) {
      event.preventDefault();
      submit();
    }
  };

  const keyShortcuts = touch ? "Meta+Enter Control+Enter" : "Enter Meta+Enter Control+Enter";
  const describedBy = problem ? `${ids}-problem` : undefined;

  return (
    <div className="flex min-h-0 flex-1 overflow-y-auto" data-testid="chat-home-start" data-agent={agent.handle}>
      <div className="m-auto flex w-full max-w-2xl flex-col items-center gap-6 px-4 py-8 md:px-6">
        <div className="flex flex-col items-center gap-3 text-center">
          <AgentAvatar agent={agent} size="xl" />
          <div className="flex flex-col gap-1">
            <h1 className="text-2xl font-semibold tracking-tight text-balance [overflow-wrap:anywhere]" data-testid="chat-home-start-title">
              {t("title", { name: agent.name })}
            </h1>
            <p className="font-mono text-xs text-muted-foreground">
              @{agent.handle}, {agent.project}
            </p>
          </div>
          {agent.description ? <p className="max-w-prose text-sm text-pretty text-muted-foreground">{agent.description}</p> : null}
        </div>
        <form onSubmit={submit} noValidate className="flex w-full flex-col gap-2" aria-busy={pending || undefined} data-testid="chat-home-composer">
          <div
            className={cn(
              "rounded-md border border-input bg-card shadow-raised transition-[border-color,box-shadow] focus-within:border-ring focus-within:ring-1 focus-within:ring-ring",
              problem && "border-danger-solid",
            )}
          >
            <label htmlFor={`${ids}-text`} className="sr-only">
              {t("label", { name: agent.name })}
            </label>
            <textarea
              ref={area}
              id={`${ids}-text`}
              value={text}
              rows={3}
              placeholder={t("placeholder", { name: agent.name })}
              onChange={(event) => {
                setText(event.target.value);
                setProblem(null);
              }}
              onKeyDown={onKeyDown}
              aria-invalid={problem ? true : undefined}
              aria-describedby={describedBy}
              aria-keyshortcuts={keyShortcuts}
              className="block max-h-60 min-h-[84px] w-full resize-y border-0 bg-transparent p-3 text-sm leading-5 text-foreground outline-none placeholder:text-fg-subtle focus-visible:outline-none max-md:text-base"
              data-testid="chat-home-composer-text"
            />
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t p-2">
              <Button type="submit" size="sm" className="ml-auto shrink-0" busy={pending} aria-keyshortcuts={keyShortcuts} data-testid="chat-home-composer-send">
                <ArrowUp aria-hidden="true" />
                {pending ? t("sending") : t("send")}
              </Button>
            </div>
          </div>
          {problem ? (
            <p id={`${ids}-problem`} className="flex items-center gap-1.5 text-xs font-medium text-danger" data-testid="chat-home-composer-problem">
              <TriangleAlert className="size-3.5 shrink-0" aria-hidden="true" />
              {problem === "blank" ? t("blank") : t("long", { max: format.number(MAX_MESSAGE_BYTES) })}
            </p>
          ) : null}
          {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
        </form>
        {agent.starters.length ? (
          <section aria-labelledby={`${ids}-starters`} className="flex w-full flex-col gap-2">
            <h2 id={`${ids}-starters`} className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
              <Sparkles className="size-3.5" aria-hidden="true" />
              {t("starters")}
            </h2>
            <ul className="grid gap-2 sm:grid-cols-2">
              {agent.starters.map((starter) => (
                <li key={starter} className="flex">
                  <button
                    type="button"
                    className="min-h-11 w-full rounded-md border bg-card px-3 py-2 text-left text-sm text-pretty text-foreground transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none [overflow-wrap:anywhere]"
                    onClick={() => {
                      setText(starter);
                      setProblem(null);
                      area.current?.focus();
                    }}
                    data-testid="chat-home-starter"
                  >
                    {starter}
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
      </div>
    </div>
  );
}
