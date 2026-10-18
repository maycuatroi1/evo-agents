"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleSlash } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import { InFrame } from "@/components/data/data-card";
import { notifyFailure } from "@/components/feedback/toast";
import { useLiveSignal } from "@/components/live/live-context";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { useMediaQuery } from "@/hooks/use-media-query";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { chatKeys, chatPageQuery, chatQuery, toggleReaction } from "@/lib/chat-queries";
import { type ChatPage, chatsHref, type GroupMessage } from "@/lib/chats";

import { ChatComposer } from "./composer";
import { ChatHeader } from "./chat-header";
import type { Feed } from "./feed-model";
import { useChatFailure, useMe } from "./hooks";
import { MembersSheet } from "./members-sheet";
import { type MessageSlot } from "./message-item";
import { MessageList } from "./message-list";
import { ThreadPanel } from "./thread-panel";
import { useChatFeed } from "./use-chat-feed";

const CARD = "flex min-h-0 flex-col overflow-hidden rounded-md border bg-card shadow-raised";
/** Tailwind's 2xl (96rem, 1536 px): from there the thread is a column beside the chat, below it a sheet. */
const THREAD_COLUMN_QUERY = "(min-width: 96rem)";

/**
 * What the page under src/app puts under a message, a hook of the live feed and whether the chat has ended: the chat
 * leaves the decisions of its agents' runs to the page, which composes the chat and the inbox's card.
 */
export type UseUnder = (feed: Feed, ended: boolean) => MessageSlot | undefined;
const useNothingUnder: UseUnder = () => undefined;

/**
 * A chat's page (web/DESIGN.md, Chats): its head, its messages, live, and the composer; a thread beside it from 2xl
 * (beside the sidebar and the list, a third column needs that much) or in a sheet below; its members in a sheet. `?thread=ID` opens a thread, and `?message=ID` (where a notice links)
 * shows that message marked, in its thread when it is a reply.
 */
export function ChatView({ chatId, initialError, useUnder = useNothingUnder }: { chatId: number; initialError: ApiErrorInfo | null; useUnder?: UseUnder }) {
  const state = useHubQuery(chatPageQuery(browserApi, chatId), initialError);
  return (
    <QueryView state={state} loading={<ChatLoading />}>
      {(page) => <Conversation key={chatId} page={page} useUnder={useUnder} />}
    </QueryView>
  );
}

function urlWith(params: Record<string, string | null>) {
  const url = new URL(window.location.href);
  for (const [key, value] of Object.entries(params)) {
    if (value === null) url.searchParams.delete(key);
    else url.searchParams.set(key, value);
  }
  window.history.replaceState(window.history.state, "", url);
}

function numberParam(value: string | null): number | null {
  return value && /^[1-9][0-9]{0,17}$/.test(value) ? Number(value) : null;
}

function Conversation({ page, useUnder }: { page: ChatPage; useUnder: UseUnder }) {
  const t = useTranslations("chats.view");
  const queryClient = useQueryClient();
  const params = useSearchParams();
  const me = useMe();
  const failure = useChatFailure();
  const chatId = page.chat.id;
  const { data: chat = page.chat } = useQuery({ ...chatQuery(browserApi, chatId), initialData: page.chat, staleTime: 30_000 });
  const onMembers = useCallback(() => void queryClient.invalidateQueries({ queryKey: chatKeys.one(chatId) }), [queryClient, chatId]);
  const live = useChatFeed({ chatId, page, me, onMembers });
  useLiveSignal(live.signal);

  const wide = useMediaQuery(THREAD_COLUMN_QUERY);
  const [thread, setThread] = useState<number | null>(() => numberParam(params.get("thread")));
  const [highlight, setHighlight] = useState<number | null>(() => numberParam(params.get("message")));
  const [members, setMembers] = useState(false);
  const opener = useRef<HTMLElement | null>(null);

  // A link to one message: a reply opens its thread; a message older than the page's opens as a thread of its own.
  const { locate } = live;
  const linked = useRef(highlight);
  useEffect(() => {
    const id = linked.current;
    if (id === null) return;
    linked.current = null;
    const oldest = page.messages[0]?.seq ?? null;
    void locate(id).then((message) => {
      if (!message) return setHighlight(null);
      const root = message.thread_root_id ?? (oldest !== null && message.seq < oldest ? message.id : null);
      if (root !== null) setThread(root);
    });
  }, [locate, page]);

  const openThread = (root: number) => {
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setThread(root);
    urlWith({ thread: String(root), message: null });
  };
  const closeThread = () => {
    setThread(null);
    setHighlight(null);
    urlWith({ thread: null, message: null });
    opener.current?.focus();
  };

  const [reacting, setReacting] = useState<number | null>(null);
  const react = useMutation({
    mutationFn: ({ message, emoji, on }: { message: GroupMessage; emoji: string; on: boolean }) => toggleReaction(browserApi(), chatId, message.id, emoji, on),
    onMutate: ({ message }) => setReacting(message.id),
    onSuccess: (message) => live.reactedTo(message),
    onError: (cause) => notifyFailure(t("reactFailed"), failure(cause)),
    onSettled: () => setReacting(null),
  });
  const onReact = (message: GroupMessage, emoji: string, on: boolean) => react.mutate({ message, emoji, on });

  const ended = live.ended !== null;
  const under = useUnder(live.feed, ended);
  const panel =
    thread !== null ? (
      <ThreadPanel
        key={thread}
        chat={chat}
        me={me}
        feed={live.feed}
        root={thread}
        highlight={highlight}
        loadThread={live.loadThread}
        onSent={live.sent}
        onClose={closeThread}
        readOnly={ended}
        onReact={onReact}
        reacting={reacting}
        under={under}
        className={wide ? `${CARD} w-[26rem] shrink-0` : "h-full"}
      />
    ) : null;

  return (
    <div className="flex h-full min-h-0 w-full gap-4" data-testid="chat-view" data-chat-id={chatId} data-stream={live.status}>
      <section aria-labelledby="chat-title" className={`${CARD} min-w-0 flex-1`}>
        <ChatHeader chat={chat} onMembers={() => setMembers(true)} />
        <MessageList
          chat={chat}
          me={me}
          feed={live.feed}
          highlight={thread === null ? highlight : null}
          loadOlder={live.loadOlder}
          onReact={onReact}
          onThread={openThread}
          reacting={reacting}
          under={under}
        />
        {ended ? (
          <div role="alert" className="flex flex-wrap items-start gap-3 border-t bg-surface-sunken px-4 py-3 text-sm" data-testid="chat-ended">
            <CircleSlash className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            <div className="flex min-w-0 flex-1 flex-col gap-0.5">
              <p className="font-medium">{t("endedTitle")}</p>
              {live.ended ? <p className="text-xs break-words text-muted-foreground">{t("hubSays", { message: live.ended })}</p> : null}
            </div>
            <Button asChild variant="outline" size="sm">
              <Link href={chatsHref()}>{t("backToChats")}</Link>
            </Button>
          </div>
        ) : (
          <div className="border-t p-3">
            <ChatComposer
              chat={chat}
              me={me}
              replyTo={null}
              onSent={live.sent}
              label={t("composerLabel", { title: chat.title })}
              placeholder={t("placeholder")}
              hint={t("hint")}
              testId="chat-composer"
            />
          </div>
        )}
      </section>
      {wide ? panel : null}
      {!wide ? (
        <Sheet open={thread !== null} onOpenChange={(open) => (open ? undefined : closeThread())}>
          <SheetContent side="right" showCloseButton={false} className="gap-0 p-0 data-[side=right]:w-full data-[side=right]:sm:max-w-lg" data-testid="chat-thread-sheet">
            <SheetTitle className="sr-only">{t("threadSheet")}</SheetTitle>
            <SheetDescription className="sr-only">{t("threadSheetBody", { title: chat.title })}</SheetDescription>
            <InFrame>{panel}</InFrame>
          </SheetContent>
        </Sheet>
      ) : null}
      <MembersSheet chat={chat} me={me} open={members} onOpenChange={setMembers} />
    </div>
  );
}

function ChatLoading() {
  return (
    <div className={`${CARD} h-full w-full`} data-testid="chat-loading">
      <div className="flex flex-col gap-2 border-b px-4 py-3">
        <Skeleton className="h-6 w-48 rounded-xs" />
        <Skeleton className="h-3 w-64 rounded-xs" />
      </div>
      <div className="flex flex-1 flex-col gap-5 p-4">
        {[0, 1, 2].map((row) => (
          <div key={row} className="flex gap-3">
            <Skeleton className="size-8 shrink-0 rounded-full" />
            <div className="flex flex-1 flex-col gap-1.5">
              <Skeleton className="h-3.5 w-40 rounded-xs" />
              <Skeleton className="h-4 w-3/4 rounded-xs" />
            </div>
          </div>
        ))}
      </div>
      <div className="border-t p-3">
        <Skeleton className="h-24 w-full rounded-md" />
      </div>
    </div>
  );
}
