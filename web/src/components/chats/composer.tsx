"use client";

import { useMutation } from "@tanstack/react-query";
import { ArrowUp, Bot, Reply, TriangleAlert, X } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, type KeyboardEvent, useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import { InlineError, type WriteFailure } from "@/components/feedback/notice";
import { shortcutText } from "@/components/shell/shortcuts";
import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { TOUCH_QUERY, useMediaQuery } from "@/hooks/use-media-query";
import { browserApi } from "@/lib/api/browser";
import { sendChatMessage } from "@/lib/chat-queries";
import {
  type Chat,
  type GroupMessage,
  handedAgents,
  insertMention,
  MAX_MESSAGE_BYTES,
  MAX_RUNS_PER_MESSAGE,
  mentionAt,
  type MentionQuery,
  mentionTargets,
  type MentionTarget,
  suggestTargets,
  utf8Bytes,
} from "@/lib/chats";
import { isChatSend, isSendShortcut, useModifierKey } from "@/lib/keyboard";
import { cn } from "@/lib/utils";

import { useChatFailure } from "./hooks";
import { MentionMenu, optionId } from "./mention-menu";

/** The byte count shows once a message passes this share of the limit. */
const COUNT_FROM = 0.75;

type Props = {
  chat: Chat;
  me: string | null;
  /** The first message of the thread the composer answers in; null for the chat itself. */
  replyTo: number | null;
  /** What the hub answered for the message sent. */
  onSent: (message: GroupMessage) => void;
  label: string;
  placeholder: string;
  /** What a message sent here does, under the box when it hands no work. */
  hint: string;
  testId: string;
  /** The text area takes the focus each time this turns non-null: the chat was opened to write in it. */
  focusKey?: string | null;
  /** The message a reply answers (`replyTo` names it): who wrote it and one line of it, above the box, and a way out. */
  replying?: Replying | null;
};

export type Replying = { name: string; text: string; onCancel: () => void };

/**
 * The kit's Composer for a chat (web/DESIGN.md, Chats): one bordered box with the focus ring on the box, the text
 * area without an edge of its own, and a bar under it that says what the message will do (the agents it hands work
 * to, by the hub's rule: the first three agents it mentions) beside Send and its key. Enter sends and Shift with Enter
 * adds a line, as in messenger apps, Cmd or Ctrl with Enter sends too, and Enter that ends an input method's word sends
 * nothing (`isChatSend`); on a touch screen Enter adds a line and Send sends. Typing `@` opens the suggestions of whom
 * it can name (`MentionMenu`), and Enter then puts the name in. A blank message or one over 16 KiB of UTF-8 is
 * refused here with the reason; a refusal of the hub shows here too, with its words. Answering one message (`replying`,
 * a direct chat's Reply), the box says which above the text, with Cancel the reply (Escape too).
 */
export function ChatComposer({ chat, me, replyTo, onSent, label, placeholder, hint, testId, focusKey = null, replying = null }: Props) {
  const t = useTranslations("chats.composer");
  const format = useFormatter();
  const ids = useId();
  const failure = useChatFailure();
  const modifier = useModifierKey();
  const touch = useMediaQuery(TOUCH_QUERY);
  const area = useRef<HTMLTextAreaElement>(null);
  const caretAfter = useRef<number | null>(null);
  const [text, setText] = useState("");
  const [at, setAt] = useState<MentionQuery | null>(null);
  const [active, setActive] = useState(0);
  const [closedAt, setClosedAt] = useState<number | null>(null);
  const [problem, setProblem] = useState<"blank" | "long" | null>(null);
  const [error, setError] = useState<WriteFailure | null>(null);

  const send = useMutation({
    mutationFn: ({ body, to }: { body: string; to: number | null }) => sendChatMessage(browserApi(), chat.id, body, to),
    onError: (cause) => setError(failure(cause)),
    onSuccess: (message) => {
      setText("");
      setAt(null);
      onSent(message);
    },
  });

  useEffect(() => {
    if (focusKey !== null) area.current?.focus();
  }, [focusKey]);

  useLayoutEffect(() => {
    if (caretAfter.current === null || !area.current) return;
    area.current.setSelectionRange(caretAfter.current, caretAfter.current);
    caretAfter.current = null;
  });

  const targets = mentionTargets(chat, me);
  const options = at && at.start !== closedAt ? suggestTargets(targets, at.query) : [];
  const open = options.length > 0;
  const current = Math.min(active, Math.max(options.length - 1, 0));
  const menuId = `${ids}-mentions`;

  const follow = (value: string, caret: number) => {
    const next = mentionAt(value, caret);
    if (next?.start !== at?.start || next?.query !== at?.query) setActive(0);
    setAt(next);
  };

  const pick = (target: MentionTarget) => {
    if (!at) return;
    const next = insertMention(text, at, target.name);
    caretAfter.current = next.caret;
    setText(next.text);
    setAt(null);
    area.current?.focus();
  };

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (send.isPending) return;
    setError(null);
    if (!text.trim()) return setProblem("blank");
    if (utf8Bytes(text) > MAX_MESSAGE_BYTES) return setProblem("long");
    setProblem(null);
    send.mutate({ body: text, to: replyTo });
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (isSendShortcut(event.nativeEvent)) {
      event.preventDefault();
      return submit();
    }
    if (!open) {
      if (isChatSend(event.nativeEvent, touch)) {
        event.preventDefault();
        return submit();
      }
      if (event.key === "Escape" && replying) {
        event.preventDefault();
        event.stopPropagation();
        replying.onCancel();
      }
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      setActive((current + step + options.length) % options.length);
    } else if ((event.key === "Enter" || event.key === "Tab") && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      pick(options[current]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      setClosedAt(at?.start ?? null);
    }
  };

  const bytes = utf8Bytes(text);
  const handed = handedAgents(text, chat);
  const over = handed.slice(MAX_RUNS_PER_MESSAGE);
  const describedBy = [replying ? `${ids}-replying` : null, `${ids}-hint`, touch ? null : `${ids}-keys`, problem ? `${ids}-problem` : null].filter(Boolean).join(" ");
  const keyShortcuts = touch ? "Meta+Enter Control+Enter" : "Enter Meta+Enter Control+Enter";

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-2" aria-busy={send.isPending || undefined} data-testid={testId}>
      <div
        className={cn(
          "relative rounded-md border border-input bg-card transition-[border-color,box-shadow] focus-within:border-ring focus-within:ring-1 focus-within:ring-ring",
          problem && "border-danger-solid",
        )}
      >
        {open ? <MentionMenu id={menuId} options={options} active={current} onPick={pick} onHover={setActive} /> : null}
        {replying ? (
          <div className="flex items-center gap-2 border-b py-1.5 pr-1.5 pl-3" data-testid={`${testId}-replying`}>
            <Reply className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
            <p id={`${ids}-replying`} className="flex min-w-0 flex-1 items-baseline gap-1.5 text-xs">
              <span className="shrink-0 font-medium text-foreground">{t("replying", { name: replying.name })}</span>{" "}
              <span className="min-w-0 truncate text-muted-foreground">{replying.text}</span>
            </p>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label={t("cancelReply")}
              title={t("cancelReply")}
              onClick={() => {
                replying.onCancel();
                area.current?.focus();
              }}
              data-testid={`${testId}-reply-cancel`}
            >
              <X aria-hidden="true" />
            </Button>
          </div>
        ) : null}
        <label htmlFor={`${ids}-text`} className="sr-only">
          {label}
        </label>
        <textarea
          ref={area}
          id={`${ids}-text`}
          value={text}
          rows={2}
          placeholder={placeholder}
          onChange={(event) => {
            setText(event.target.value);
            setProblem(null);
            follow(event.target.value, event.target.selectionStart);
          }}
          onSelect={(event) => follow(event.currentTarget.value, event.currentTarget.selectionStart)}
          onBlur={() => setAt(null)}
          onKeyDown={onKeyDown}
          aria-autocomplete="list"
          aria-controls={open ? menuId : undefined}
          aria-activedescendant={open ? optionId(menuId, current) : undefined}
          aria-invalid={problem ? true : undefined}
          aria-describedby={describedBy}
          aria-keyshortcuts={keyShortcuts}
          className="block max-h-48 min-h-[60px] w-full resize-y border-0 bg-transparent p-3 text-sm leading-5 text-foreground outline-none placeholder:text-fg-subtle focus-visible:outline-none max-md:text-base"
          data-testid={`${testId}-text`}
        />
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t p-2">
          <p id={`${ids}-hint`} className="flex min-w-0 flex-1 basis-48 items-start gap-1.5 pl-1 text-xs text-pretty text-fg-subtle" data-testid={`${testId}-hint`}>
            {handed.length ? (
              <>
                <Bot className="mt-px size-3.5 shrink-0 text-running" aria-hidden="true" />
                <span>
                  {t("hands", { handles: format.list(handed.slice(0, MAX_RUNS_PER_MESSAGE).map((agent) => `@${agent.handle}`), { type: "conjunction" }) })}
                  {over.length ? (
                    <span className="text-attention"> {t("over", { max: MAX_RUNS_PER_MESSAGE, handles: format.list(over.map((agent) => `@${agent.handle}`), { type: "conjunction" }) })}</span>
                  ) : null}
                </span>
              </>
            ) : (
              <span>{hint}</span>
            )}
          </p>
          {touch ? null : (
            <span id={`${ids}-keys`} className="sr-only">
              {t("keys")}
            </span>
          )}
          {/* Announces how many names the suggestions offer as they change. */}
          <span aria-live="polite" className="sr-only">
            {open ? t("mention.count", { count: options.length }) : ""}
          </span>
          {bytes >= MAX_MESSAGE_BYTES * COUNT_FROM ? (
            <span className={cn("text-xs text-muted-foreground tabular-nums", bytes > MAX_MESSAGE_BYTES && "font-medium text-danger")}>
              {t("bytes", { count: format.number(bytes), max: format.number(MAX_MESSAGE_BYTES) })}
            </span>
          ) : null}
          <Button type="submit" size="sm" className="ml-auto shrink-0" busy={send.isPending} aria-keyshortcuts={keyShortcuts} data-testid={`${testId}-send`}>
            <ArrowUp aria-hidden="true" />
            {send.isPending ? t("sending") : t("send")}
            {modifier && !touch && !send.isPending ? (
              <Kbd className="ml-0.5 border-current bg-transparent text-current opacity-70" aria-hidden="true">
                {shortcutText("chatSend", modifier)}
              </Kbd>
            ) : null}
          </Button>
        </div>
      </div>
      {problem ? (
        <p id={`${ids}-problem`} className="flex items-center gap-1.5 text-xs font-medium text-danger" data-testid={`${testId}-problem`}>
          <TriangleAlert className="size-3.5 shrink-0" aria-hidden="true" />
          {problem === "blank" ? t("blank") : t("long", { max: format.number(MAX_MESSAGE_BYTES) })}
        </p>
      ) : null}
      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
    </form>
  );
}
