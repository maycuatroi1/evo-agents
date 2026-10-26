"use client";

import { CornerDownRight } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { type ChatAuthor, type GroupMessage, isMine } from "@/lib/chats";
import { type MentionMark, plainMentions } from "@/lib/mention-marks";
import { cn } from "@/lib/utils";

import { AuthorName } from "./author";
import { AuthorAvatar } from "./faces";
import { authorKey, preview, type Quote } from "./line-model";

/**
 * What heads a message (web/DESIGN.md, Chats), named by the message's `aria-labelledby`: another's name and the time,
 * the visitor's own time alone (no face, no name: "You" for a screen reader). A message that runs on from the one above
 * shows no head; a screen reader still hears who wrote it and when.
 */
export function MessageHead({ id, message, me, mine, runOn }: { id: string; message: GroupMessage; me: string | null; mine: boolean; runOn: boolean }) {
  const t = useTranslations("chats.message");
  const format = useFormatter();
  const created = new Date(message.created_at);
  const time = (
    <time
      dateTime={message.created_at}
      title={format.dateTime(created, { dateStyle: "medium", timeStyle: "short" })}
      className="text-xs whitespace-nowrap text-fg-subtle tabular-nums"
    >
      {format.dateTime(created, { timeStyle: "short" })}
    </time>
  );
  if (runOn || mine) {
    return (
      <p id={id} className={cn("flex", runOn ? "sr-only" : "justify-end px-1")}>
        {mine ? <span className="sr-only">{t("youName")}</span> : <AuthorName author={message.author} me={me} />}
        <span className="sr-only">, </span>
        {time}
      </p>
    );
  }
  return (
    <p id={id} className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5 px-1">
      <AuthorName author={message.author} me={me} />
      {time}
    </p>
  );
}

/** Who a message is by, in a few words: You, an agent's name, a person's login, the hub. */
export function useAuthorLabel() {
  const t = useTranslations("chats.message");
  return (author: ChatAuthor, me: string | null) => {
    if (isMine(author, me)) return t("youName");
    if (author.kind === "hub") return t("hub");
    return author.kind === "agent" ? author.name : (author.login ?? author.name);
  };
}

/**
 * In a direct chat, above an answer that does not sit right under what it answers: that message, its author and one
 * line of its words, which shows it (in the line, or its thread when the page does not hold it).
 */
export function ReplyQuote({ quote, me, marks, onShow }: { quote: Quote; me: string | null; marks: readonly MentionMark[]; onShow: (quote: Quote) => void }) {
  const t = useTranslations("chats.message");
  const label = useAuthorLabel();
  const answered = quote.message;
  const name = answered ? label(answered.author, me) : null;
  const text = answered ? plainMentions(preview(answered.body), marks) : null;
  return (
    <button
      type="button"
      onClick={() => onShow(quote)}
      aria-label={answered ? t("quoteLabel", { name: name ?? "", text: text ?? "" }) : t("quoteEarlierLabel")}
      className="flex max-w-full min-w-0 cursor-pointer items-center gap-1.5 rounded-xs border-l-2 border-border-strong py-0.5 pr-1.5 pl-2 text-left text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground max-md:min-h-11"
      data-testid="message-quote"
      data-quote-id={answered?.id ?? quote.root}
    >
      <CornerDownRight className="size-3.5 shrink-0" aria-hidden="true" />
      {answered ? (
        <>
          <span className="shrink-0 font-medium text-foreground">{name}</span>
          <span className="min-w-0 truncate">{text}</span>
        </>
      ) : (
        <span className="min-w-0 truncate">{t("quoteEarlier")}</span>
      )}
    </button>
  );
}

/** "3 replies": the faces of who wrote them and when the last came, opening the thread. */
export function ThreadSummary({
  root,
  count,
  last,
  authors,
  onOpen,
}: {
  root: number;
  count: number;
  last: GroupMessage | null;
  authors: ChatAuthor[];
  onOpen: (root: number) => void;
}) {
  const t = useTranslations("chats.message");
  const format = useFormatter();
  return (
    <button
      type="button"
      onClick={() => onOpen(root)}
      className="inline-flex w-fit max-w-full cursor-pointer items-center gap-2 rounded-sm px-1 py-0.5 text-left text-xs transition-colors hover:bg-accent max-md:min-h-11"
      data-testid="thread-summary"
      data-replies={count}
    >
      <span className="flex -space-x-1.5" aria-hidden="true">
        {authors.slice(0, 3).map((author) => (
          <AuthorAvatar key={authorKey(author)} author={author} size="sm" className="ring-2 ring-card" />
        ))}
      </span>
      <span className="font-medium text-brand">{t("replies", { count })}</span>
      {last ? (
        <span className="truncate text-fg-subtle">{t("lastReply", { time: format.dateTime(new Date(last.created_at), { timeStyle: "short" }) })}</span>
      ) : null}
    </button>
  );
}
