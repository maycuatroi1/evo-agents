"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, ArrowUp, Bot, CircleCheck, FileText, MessageCircleOff, MessageSquare, RotateCw, Square, User, WifiOff } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useId, useLayoutEffect, useRef, useState } from "react";

import { ConfirmAction } from "@/components/admin/confirm-action";
import { InlineError, type WriteFailure } from "@/components/admin/notice";
import { notify } from "@/components/feedback/toast";
import { SafeMarkdown } from "@/components/memories/markdown";
import { planHref } from "@/components/plans/links";
import { shortcutText } from "@/components/shell/shortcuts";
import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { isSendShortcut, useModifierKey } from "@/lib/keyboard";
import { cn } from "@/lib/utils";

import { useRunFailure } from "./hooks";
import {
  type ChatMessage,
  chatKey,
  chatQuery,
  type ChatStatus,
  finishChat,
  MAX_MESSAGE_BYTES,
  type Run,
  runHref,
  runKeys,
  type RunChat,
  sendRunMessage,
} from "./queries";
import { chatEndable, chatReplyable, utf8Bytes } from "./run-model";

/** The byte count shows once a reply passes this share of the limit. */
const COUNT_FROM = 0.75;

const STATUS_LOOK: Record<ChatStatus, string> = {
  working: "border-running/30 bg-running-soft text-running",
  waiting: "border-attention/30 bg-attention-soft text-attention",
  ended: "border-border bg-muted text-muted-foreground",
};

/**
 * The Chat tab of an author run (docs/workers.md, Author runs): the member's request, then the agent's messages and
 * the owner's replies in order, over the run and the runs that resume it (`GET .../chat`, asked again every 3 seconds
 * until it ended, and whenever the run's stream brings an event); whose turn it is (the agent works, it waits for you,
 * the chat ended); the plan the run wrote or revises; the owner's reply box, which posts to the run that takes the
 * next message; and End chat, which ends the run done once the agent's turn is over.
 */
export function RunChatPanel({ run, owner, shown }: { run: Run; owner: boolean; shown: boolean }) {
  const t = useTranslations("runs.author.chat");
  const format = useFormatter();
  const chat = useQuery(chatQuery(browserApi, run.project, run.id));
  const scrollRef = useRef<HTMLDivElement>(null);
  const count = chat.data?.messages.length ?? 0;

  // The newest message in view when one arrives, and when the tab is shown again (hidden, it cannot scroll).
  useLayoutEffect(() => {
    const element = scrollRef.current;
    if (element && shown) element.scrollTop = element.scrollHeight;
  }, [count, shown, chat.data?.status]);

  if (chat.isPending) {
    return (
      <div className="flex flex-col gap-3 p-4" role="status" aria-label={t("loading")} data-testid="run-chat-loading">
        <Skeleton className="h-9 w-full" />
        <Skeleton className="h-20 w-3/4" />
        <Skeleton className="ml-auto h-12 w-2/3" />
      </div>
    );
  }
  if (chat.isError && !chat.data) {
    return (
      <div className="flex flex-col items-start gap-3 p-4" role="alert" data-testid="run-chat-error">
        <p className="flex items-start gap-2 text-sm text-danger">
          <MessageCircleOff className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          {t("loadFailed")}
        </p>
        <Button type="button" variant="outline" size="sm" onClick={() => void chat.refetch()} data-testid="run-chat-retry">
          <RotateCw aria-hidden="true" />
          {t("retry")}
        </Button>
      </div>
    );
  }
  const data = chat.data!;
  const stale = chat.isError; // a read failed after one worked: what shows is the chat as last read
  return (
    <div className="flex min-w-0 flex-col" data-testid="run-chat" data-status={data.status} data-run-id={data.run_id}>
      <ChatHead run={run} chat={data} owner={owner} />
      {stale ? (
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b bg-danger-soft px-4 py-2 text-xs text-danger" role="status" data-testid="run-chat-offline">
          <WifiOff className="size-3.5 shrink-0" aria-hidden="true" />
          <span className="min-w-0 flex-1">
            {t("offline", { time: format.dateTime(new Date(chat.dataUpdatedAt), { timeStyle: "medium" }) })}
          </span>
          <Button type="button" variant="ghost" size="sm" className="h-7 text-danger" onClick={() => void chat.refetch()}>
            <RotateCw aria-hidden="true" />
            {t("retry")}
          </Button>
        </p>
      ) : null}
      <div
        ref={scrollRef}
        role="log"
        aria-live="polite"
        aria-relevant="additions"
        aria-label={t("label", { id: run.id })}
        tabIndex={0}
        className="flex max-h-[min(65vh,40rem)] min-h-64 flex-col gap-4 overflow-y-auto overscroll-contain px-4 py-4 focus-visible:outline-offset-[-2px]"
        data-testid="run-chat-messages"
      >
        {data.more ? <p className="text-center text-xs text-fg-subtle">{t("more")}</p> : null}
        {run.request ? <Message author="owner" name={t("request")} at={run.queued_at} text={run.request} testId="run-chat-request" /> : null}
        {data.messages.map((message, index) => (
          <ChatLine
            key={`${message.run_id}:${message.seq}`}
            message={message}
            viewer={owner}
            asks={data.status === "waiting" && message.author === "agent" && index === data.messages.length - 1}
          />
        ))}
        {data.messages.length === 0 && data.status === "working" ? (
          <p className="text-sm text-pretty text-muted-foreground" data-testid="run-chat-empty">
            {run.state === "queued" ? t("emptyQueued") : t("emptyWorking")}
          </p>
        ) : null}
        {data.messages.length === 0 && data.status === "ended" ? (
          <p className="text-sm text-pretty text-muted-foreground" data-testid="run-chat-empty">
            {t("emptyEnded")}
          </p>
        ) : null}
        {data.status === "working" && run.state !== "queued" ? <Typing /> : null}
      </div>
      {owner && chatReplyable(data, run, data.run_id) ? <ChatReply project={run.project} chat={data} /> : null}
      {!owner && data.status !== "ended" ? (
        <p className="border-t px-4 py-3 text-xs text-pretty text-fg-subtle" data-testid="run-chat-readonly">
          {t("readOnly", { login: data.owner })}
        </p>
      ) : null}
    </div>
  );
}

/** Whose turn it is, the plan the run wrote or revises, the run the chat goes on in, and End chat for the owner. */
function ChatHead({ run, chat, owner }: { run: Run; chat: RunChat; owner: boolean }) {
  const t = useTranslations("runs.author.chat");
  const ending = Boolean(run.finish_requested_at) && chat.status !== "ended";
  const status = (
    <span
      className={cn("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium", STATUS_LOOK[chat.status])}
      data-testid="run-chat-status"
      data-status={chat.status}
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
      {ending ? t("status.ending") : t(`status.${chat.status}`, { login: chat.owner, you: owner ? "yes" : "no" })}
    </span>
  );
  return (
    <div className="flex flex-col gap-2 border-b px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span role="status" className="inline-flex">
          {status}
        </span>
        <PlanLink run={run} chat={chat} />
        {owner && chatEndable(chat, run) ? <EndChat run={run} chat={chat} /> : null}
      </div>
      {chat.run_id !== run.id ? (
        <p className="text-xs text-pretty text-muted-foreground" data-testid="run-chat-continues">
          {t.rich("continues", {
            id: chat.run_id,
            link: (chunks) => (
              <Link href={runHref(run.project, chat.run_id)} className="font-medium text-brand underline underline-offset-4">
                {chunks}
              </Link>
            ),
          })}
        </p>
      ) : null}
    </div>
  );
}

function PlanLink({ run, chat }: { run: Run; chat: RunChat }) {
  const t = useTranslations("runs.author.chat");
  if (!chat.plan_id) {
    return (
      <span className="inline-flex min-w-0 items-center gap-1.5 text-xs text-fg-subtle" data-testid="run-chat-no-plan">
        <FileText className="size-3.5 shrink-0" aria-hidden="true" />
        {t("noPlan")}
      </span>
    );
  }
  return (
    <Link
      href={planHref(run.project, chat.plan_id)}
      className="inline-flex min-w-0 items-center gap-1.5 rounded-xs text-[13px] font-medium text-brand underline decoration-brand/40 underline-offset-4 hover:decoration-current"
      data-testid="run-chat-plan"
      data-plan-id={chat.plan_id}
      data-revision={chat.plan_revision ?? undefined}
    >
      <FileText className="size-3.5 shrink-0" aria-hidden="true" />
      <span className="min-w-0 [overflow-wrap:anywhere]">
        {chat.plan_revision !== null
          ? t("plan", { plan: chat.plan_id, revision: chat.plan_revision })
          : t("planNoRevision", { plan: chat.plan_id })}
      </span>
      <ArrowRight className="size-3.5 shrink-0" aria-hidden="true" />
    </Link>
  );
}

function ChatLine({ message, viewer, asks }: { message: ChatMessage; viewer: boolean; asks: boolean }) {
  const t = useTranslations("runs.author.chat");
  const name = message.author === "agent" ? t("agent") : viewer ? t("you") : (message.login ?? t("owner"));
  return (
    <Message
      author={message.author}
      name={name}
      at={message.at}
      text={message.text}
      testId={message.author === "agent" ? "run-chat-agent" : "run-chat-owner"}
      tag={asks ? <span className="text-xs font-medium text-attention" data-testid="run-chat-asks">{t("asks")}</span> : null}
    />
  );
}

function Message({
  author,
  name,
  at,
  text,
  testId,
  tag = null,
}: {
  author: ChatMessage["author"];
  name: string;
  at: string;
  text: string;
  testId: string;
  tag?: ReactNode;
}) {
  const format = useFormatter();
  const agent = author === "agent";
  const date = new Date(at);
  return (
    <article className={cn("flex min-w-0 gap-3", !agent && "flex-row-reverse")} data-testid={testId} data-author={author}>
      <span
        className={cn(
          "grid size-7 shrink-0 place-items-center rounded-full border",
          agent ? "border-running bg-card text-running" : "border-border bg-surface-sunken text-muted-foreground",
        )}
        aria-hidden="true"
      >
        {agent ? <Bot className="size-4" /> : <User className="size-4" />}
      </span>
      <div className={cn("flex min-w-0 max-w-[min(100%,46rem)] flex-col gap-1", !agent && "items-end")}>
        <header className={cn("flex flex-wrap items-baseline gap-x-2 gap-y-0.5", !agent && "flex-row-reverse")}>
          <h3 className="text-[13px] leading-[18px] font-semibold">{name}</h3>
          <time dateTime={at} className="text-xs text-fg-subtle tabular-nums" title={format.dateTime(date, { dateStyle: "full", timeStyle: "long" })}>
            {format.dateTime(date, { dateStyle: "short", timeStyle: "short" })}
          </time>
          {tag}
        </header>
        <div className={cn("min-w-0 max-w-full rounded-md border px-3 py-2", agent ? "bg-card" : "border-brand/20 bg-surface-selected")}>
          {agent ? (
            <SafeMarkdown className="leading-[21px]" testId="run-chat-text">
              {text}
            </SafeMarkdown>
          ) : (
            <p className="text-sm whitespace-pre-wrap text-foreground [overflow-wrap:anywhere]" data-testid="run-chat-text">
              {text}
            </p>
          )}
        </div>
      </div>
    </article>
  );
}

function Typing() {
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

/**
 * The owner's reply: the kit's Composer, posting to the run that takes the chat's next message (a parked run's reply
 * queues the run that resumes it). A blank reply or one over 8 KiB of UTF-8 is refused here; the hub's refusal shows
 * under the box.
 */
function ChatReply({ project, chat }: { project: string; chat: RunChat }) {
  const t = useTranslations("runs.author.chat.reply");
  const format = useFormatter();
  const ids = useId();
  const failure = useRunFailure();
  const modifier = useModifierKey();
  const queryClient = useQueryClient();
  const [text, setText] = useState("");
  const [problem, setProblem] = useState<"blank" | "long" | null>(null);
  const [error, setError] = useState<WriteFailure | null>(null);
  const [sent, setSent] = useState(false);
  const send = useMutation({
    mutationFn: (message: string) => sendRunMessage(browserApi(), project, chat.run_id, message),
    onError: (cause) => {
      if (isApiError(cause) && cause.kind === "unauthorized") window.location.assign(LOGIN_PATH);
      setError(failure(cause, t("conflict")));
    },
    onSuccess: () => {
      setText("");
      setSent(true);
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: runKeys.all(project) }),
  });
  const bytes = utf8Bytes(text);
  const long = bytes > MAX_MESSAGE_BYTES;

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (send.isPending) return;
    setSent(false);
    setError(null);
    if (!text.trim()) return setProblem("blank");
    if (long) return setProblem("long");
    setProblem(null);
    send.mutate(text);
  };

  const describedBy = [`${ids}-hint`, problem ? `${ids}-problem` : null].filter(Boolean).join(" ");
  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-2 border-t px-4 pt-3 pb-4" aria-busy={send.isPending || undefined} data-testid="run-chat-reply">
      <div
        className={cn(
          "rounded-md border border-input bg-card transition-[border-color,box-shadow] focus-within:border-ring focus-within:ring-1 focus-within:ring-ring",
          problem && "border-danger-solid",
        )}
      >
        <label htmlFor={`${ids}-text`} className="sr-only">
          {t("label")}
        </label>
        <textarea
          id={`${ids}-text`}
          value={text}
          rows={3}
          placeholder={chat.status === "waiting" ? t("placeholderWaiting") : t("placeholderWorking")}
          onChange={(event) => {
            setText(event.target.value);
            setProblem(null);
            setSent(false);
          }}
          onKeyDown={(event) => {
            if (isSendShortcut(event.nativeEvent)) {
              event.preventDefault();
              submit();
            }
          }}
          className="block max-h-60 min-h-[76px] w-full resize-y border-0 bg-transparent p-3 text-sm leading-5 text-foreground outline-none placeholder:text-fg-subtle focus-visible:outline-none max-sm:text-base"
          aria-invalid={problem ? true : undefined}
          aria-describedby={describedBy}
          aria-keyshortcuts="Meta+Enter Control+Enter"
          data-testid="run-chat-reply-text"
        />
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t p-2">
          <p id={`${ids}-hint`} className="min-w-0 flex-1 basis-48 pl-1 text-xs text-pretty text-fg-subtle">
            {chat.status === "waiting" ? (chat.state === "parked" ? t("hintParked") : t("hintWaiting")) : t("hintWorking")}
          </p>
          <span aria-live="polite" className="text-xs font-medium text-success empty:hidden" data-testid="run-chat-reply-sent">
            {sent ? (
              <span className="inline-flex items-center gap-1.5">
                <CircleCheck className="size-3.5 shrink-0" aria-hidden="true" />
                {t("sent")}
              </span>
            ) : null}
          </span>
          {bytes >= MAX_MESSAGE_BYTES * COUNT_FROM ? (
            <span className={cn("text-xs text-muted-foreground tabular-nums", long && "font-medium text-danger")}>
              {t("bytes", { count: format.number(bytes), max: format.number(MAX_MESSAGE_BYTES) })}
            </span>
          ) : null}
          <Button type="submit" size="sm" className="ml-auto shrink-0" busy={send.isPending} aria-keyshortcuts="Meta+Enter Control+Enter" data-testid="run-chat-send">
            <ArrowUp aria-hidden="true" />
            {send.isPending ? t("sending") : t("send")}
            {modifier && !send.isPending ? (
              <Kbd className="ml-0.5 border-current bg-transparent text-current opacity-70" aria-hidden="true">
                {shortcutText("send", modifier)}
              </Kbd>
            ) : null}
          </Button>
        </div>
      </div>
      {problem ? (
        <p id={`${ids}-problem`} className="text-xs font-medium text-danger" data-testid="run-chat-reply-problem">
          {problem === "blank" ? t("blank") : t("long", { max: format.number(MAX_MESSAGE_BYTES) })}
        </p>
      ) : null}
      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
    </form>
  );
}

/** End chat: confirmed first, since a chat that ended takes no more replies; the run ends done after the agent's turn. */
function EndChat({ run, chat }: { run: Run; chat: RunChat }) {
  const t = useTranslations("runs.author.chat.end");
  const failure = useRunFailure();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<WriteFailure | null>(null);
  const end = useMutation({
    mutationFn: () => finishChat(browserApi(), run.project, chat.run_id),
    onError: (cause) => {
      if (isApiError(cause) && cause.kind === "unauthorized") window.location.assign(LOGIN_PATH);
      setError(failure(cause, t("conflict")));
    },
    onSuccess: (answer) => {
      setOpen(false);
      notify({ tone: "success", text: t("toast"), description: answer.state === "done" ? t("toastDone") : t("toastAfterTurn") });
    },
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: runKeys.all(run.project) }),
        queryClient.invalidateQueries({ queryKey: chatKey(run.project, run.id) }),
      ]),
  });
  return (
    <>
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="ml-auto"
        onClick={() => {
          setError(null);
          setOpen(true);
        }}
        data-testid="run-chat-end"
      >
        <Square aria-hidden="true" />
        {t("button")}
      </Button>
      <ConfirmAction
        open={open}
        onOpenChange={(next) => {
          setOpen(next);
          if (!next) setError(null);
        }}
        title={t("title", { id: run.id })}
        description={<p className="text-pretty">{chat.state === "parked" ? t("descriptionParked") : t("description")}</p>}
        confirmLabel={t("confirm")}
        pendingLabel={t("pending")}
        cancelLabel={t("keep")}
        pending={end.isPending}
        error={error}
        onConfirm={() => end.mutate()}
        testId="run-chat-end-dialog"
      />
    </>
  );
}
