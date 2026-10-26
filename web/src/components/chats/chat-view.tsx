"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleSlash } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import { FileDropFrame } from "@/components/data/attachments";
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
import { chatKeys, chatPageQuery, chatQuery, COMPOSE_PARAM, toggleReaction } from "@/lib/chat-queries";
import { type Chat, type ChatPage, chatsHref, type ChatSummary, type GroupMessage } from "@/lib/chats";
import { chatMentionMarks, plainMentions } from "@/lib/mention-marks";

import { AgentActionsContext, type AgentTab, type AgentTarget } from "./agent-menu";
import type { AgentPanels } from "./agent-sheet";
import { ChatComposer } from "./composer";
import { ChatHeader } from "./chat-header";
import { OpenDocContext } from "./doc-model";
import { DocPanel } from "./doc-panel";
import type { Feed } from "./feed-model";
import { useChatFailure, useMe } from "./hooks";
import { preview, type Quote } from "./line-model";
import { type MessageSlot } from "./message-item";
import { MessageList } from "./message-list";
import { useAuthorLabel } from "./message-parts";
import { ThreadPanel } from "./thread-panel";
import { useChatFeed } from "./use-chat-feed";

/** The members' sheet opens on demand: its code loads when the chat's head asks for it. */
const MembersSheet = dynamic(() => import("./members-sheet").then((module) => module.MembersSheet), { ssr: false });
/** The agent sheet too, which an agent's menu opens. */
const AgentSheet = dynamic(() => import("./agent-sheet").then((module) => module.AgentSheet), { ssr: false });

const CARD = "flex min-h-0 flex-col overflow-hidden rounded-md border bg-card shadow-raised";
/** Tailwind's 2xl (96rem, 1536 px): from there the thread is a column beside the chat, below it a sheet. */
const THREAD_COLUMN_QUERY = "(min-width: 96rem)";
/** Tailwind's lg (64rem): from there a document is a column beside the chat, below it a sheet. */
const DOC_COLUMN_QUERY = "(min-width: 64rem)";

/**
 * What the page under src/app puts under a message, a hook of the live feed and whether the chat has ended: the chat
 * leaves the decisions of its agents' runs to the page, which composes the chat and the inbox's card.
 */
export type UseUnder = (feed: Feed, ended: boolean) => MessageSlot | undefined;
const useNothingUnder: UseUnder = () => undefined;

/**
 * A chat's page (web/DESIGN.md, Chats): its head, its messages, live, and the composer; a thread beside it from 2xl
 * (beside the sidebar and the list, a third column needs that much) or in a sheet below; its members in a sheet. A
 * Markdown document of a message opens beside the chat from lg and in a sheet below (doc-panel.tsx); while it is
 * a column, a thread opens in its sheet. `?thread=ID` opens a thread, and `?message=ID` (where a notice links)
 * shows that message marked, in its thread when it is a reply of a group chat or older than the page read.
 * `?compose=1` (where an agent's avatar and its Chat button lead) focuses the composer, then leaves the address. In a
 * direct chat Reply answers a message from the chat's own composer, in its thread, so the agent's session goes on.
 * With `agentPanels`, an agent's face or name opens its menu (`agent-menu.tsx`), whose Workers and pools and Accounts
 * open the agent sheet (`agent-sheet.tsx`) on that tab, drawn by the page.
 */
export function ChatView({
  chatId,
  initialError,
  useUnder = useNothingUnder,
  agentPanels,
}: {
  chatId: number;
  initialError: ApiErrorInfo | null;
  useUnder?: UseUnder;
  agentPanels?: AgentPanels;
}) {
  const state = useHubQuery(chatPageQuery(browserApi, chatId), initialError);
  return (
    <QueryView state={state} loading={<ChatLoading />}>
      {(page) => <Conversation key={chatId} page={page} useUnder={useUnder} agentPanels={agentPanels} />}
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

function Conversation({ page, useUnder, agentPanels }: { page: ChatPage; useUnder: UseUnder; agentPanels?: AgentPanels }) {
  const t = useTranslations("chats.view");
  const tDoc = useTranslations("chats.doc");
  const queryClient = useQueryClient();
  const params = useSearchParams();
  const me = useMe();
  const failure = useChatFailure();
  const chatId = page.chat.id;
  const { data: chat = page.chat } = useQuery({ ...chatQuery(browserApi, chatId), initialData: page.chat, staleTime: 30_000 });
  const onMembers = useCallback(() => void queryClient.invalidateQueries({ queryKey: chatKeys.one(chatId) }), [queryClient, chatId]);
  // Another member changed the chat's image: the head shows it now, and the list once it reads the chats again.
  const onImage = useCallback(
    (imageUrl: string | null) => {
      queryClient.setQueryData<Chat>(chatKeys.one(chatId), (held) => (held ? { ...held, image_url: imageUrl } : held));
      queryClient.setQueryData<ChatSummary[]>(chatKeys.list, (list) => list?.map((item) => (item.id === chatId ? { ...item, image_url: imageUrl } : item)));
    },
    [queryClient, chatId],
  );
  const live = useChatFeed({ chatId, page, me, onMembers, onImage });

  // Opened to write (an agent's avatar or its Chat button): the composer takes the focus, and the address drops it.
  const compose = params.get(COMPOSE_PARAM);
  useEffect(() => {
    if (compose !== null) urlWith({ [COMPOSE_PARAM]: null });
  }, [compose]);
  useLiveSignal(live.signal);

  const wide = useMediaQuery(THREAD_COLUMN_QUERY);
  const [thread, setThread] = useState<number | null>(() => numberParam(params.get("thread")));
  const [highlight, setHighlight] = useState<number | null>(() => numberParam(params.get("message")));
  const [members, setMembers] = useState(false);
  // The agent sheet, on the agent and tab its menu picked: an agent of the chat brings whether it is its project's
  // default; the members' sheet closes, so one sheet shows at a time.
  const [agentSheet, setAgentSheet] = useState<{ agent: AgentTarget; tab: AgentTab } | null>(null);
  const openAgent = useCallback(
    (agent: AgentTarget, tab: AgentTab) => {
      const known = chat.agents.find((each) => each.handle === agent.handle && each.project === agent.project);
      setMembers(false);
      setAgentSheet({ agent: known ? { ...agent, is_default: known.is_default } : agent, tab });
    },
    [chat.agents],
  );
  const opener = useRef<HTMLElement | null>(null);
  // The Markdown document read beside the chat (doc-panel.tsx), by its name, and what opened it: the composer stays in
  // reach, and Close gives the focus back.
  const [doc, setDoc] = useState<string | null>(null);
  const docWide = useMediaQuery(DOC_COLUMN_QUERY);
  const docOpener = useRef<HTMLElement | null>(null);
  const openDoc = useCallback((name: string) => {
    docOpener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setDoc(name);
  }, []);
  const closeDoc = () => {
    setDoc(null);
    docOpener.current?.focus();
  };

  // A link to one message: a reply of a group chat opens its thread (a direct chat's sits in the line); a message older
  // than the page's opens in its thread, or as a thread of its own.
  const { locate } = live;
  const linked = useRef(highlight);
  useEffect(() => {
    const id = linked.current;
    if (id === null) return;
    linked.current = null;
    const oldest = page.messages[0]?.seq ?? null;
    void locate(id).then((message) => {
      if (!message) return setHighlight(null);
      const older = oldest !== null && message.seq < oldest;
      const root = page.chat.kind === "direct" ? (older ? (message.thread_root_id ?? message.id) : null) : (message.thread_root_id ?? (older ? message.id : null));
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

  // A direct chat's Reply: the composer answers that message, in its thread, and takes the focus each time.
  const authorLabel = useAuthorLabel();
  const [replying, setReplying] = useState<GroupMessage | null>(null);
  const [replyFocus, setReplyFocus] = useState(0);
  const replyTo = (message: GroupMessage) => {
    setReplying(message);
    setReplyFocus((count) => count + 1);
  };
  // A quote shows the message it names where it sits in the line, marked; one older than the page, in its thread.
  const showQuote = (quote: Quote) => {
    const target = quote.message;
    const element = target ? document.getElementById(`message-${target.id}`) : null;
    if (!target || !element) return openThread(quote.root);
    setHighlight(target.id);
    element.scrollIntoView({ block: "center" });
    element.focus({ preventScroll: true });
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
  const direct = chat.kind === "direct";
  const agentName = chat.agents[0]?.name ?? chat.title;
  // An open document takes the column beside the chat: the thread opens in its sheet meanwhile.
  const threadColumn = wide && doc === null;
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
        className={threadColumn ? `${CARD} w-[26rem] shrink-0` : "h-full"}
      />
    ) : null;

  const docPanel =
    doc !== null ? (
      <DocPanel key={doc} feed={live.feed} name={doc} me={me} onClose={closeDoc} className={docWide ? `${CARD} w-[min(44rem,48%)] shrink-0` : "h-full"} />
    ) : null;

  return (
    <OpenDocContext.Provider value={openDoc}>
    <AgentActionsContext.Provider value={agentPanels ? openAgent : null}>
    <div className="flex h-full min-h-0 w-full gap-4" data-testid="chat-view" data-chat-id={chatId} data-stream={live.status}>
      <section aria-labelledby="chat-title" className={`${CARD} min-w-0 flex-1`}>
        <FileDropFrame className="flex min-h-0 flex-1 flex-col" data-testid="chat-drop">
          <ChatHeader chat={chat} me={me} onMembers={() => setMembers(true)} />
          <MessageList
            chat={chat}
            me={me}
            feed={live.feed}
            highlight={thread === null ? highlight : null}
            loadOlder={live.loadOlder}
            onReact={onReact}
            onThread={openThread}
            onReply={replyTo}
            onQuote={showQuote}
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
                replyTo={replying?.id ?? null}
                onSent={(message) => {
                  live.sent(message);
                  setReplying(null);
                }}
                label={t("composerLabel", { title: chat.title })}
                placeholder={direct ? t("directPlaceholder", { name: agentName }) : t("placeholder")}
                hint={direct ? t(replying ? "directReplyHint" : "directHint", { name: agentName }) : t("hint")}
                testId="chat-composer"
                focusKey={replying ? `reply-${replyFocus}` : compose}
                replying={replying ? { name: authorLabel(replying.author, me), text: plainMentions(preview(replying.body), chatMentionMarks(chat)), onCancel: () => setReplying(null) } : null}
              />
            </div>
          )}
        </FileDropFrame>
      </section>
      {docWide ? docPanel : null}
      {threadColumn ? panel : null}
      {!threadColumn ? (
        <Sheet open={thread !== null} onOpenChange={(open) => (open ? undefined : closeThread())}>
          <SheetContent side="right" showCloseButton={false} className="gap-0 p-0 data-[side=right]:w-full data-[side=right]:sm:max-w-lg" data-testid="chat-thread-sheet">
            <SheetTitle className="sr-only">{t("threadSheet")}</SheetTitle>
            <SheetDescription className="sr-only">{t("threadSheetBody", { title: chat.title })}</SheetDescription>
            <InFrame>{panel}</InFrame>
          </SheetContent>
        </Sheet>
      ) : null}
      {!docWide ? (
        <Sheet open={doc !== null} onOpenChange={(open) => (open ? undefined : closeDoc())}>
          <SheetContent
            side="right"
            showCloseButton={false}
            className="gap-0 p-0 data-[side=right]:w-full data-[side=right]:sm:max-w-lg"
            onCloseAutoFocus={(event) => {
              // The sheet traps the focus until it has closed: only then can the button that opened it take it back.
              if (docOpener.current?.isConnected) {
                event.preventDefault();
                docOpener.current.focus();
              }
            }}
            data-testid="chat-doc-sheet"
          >
            <SheetTitle className="sr-only">{tDoc("sheet")}</SheetTitle>
            <SheetDescription className="sr-only">{tDoc("sheetBody", { name: doc ?? "", title: chat.title })}</SheetDescription>
            <InFrame>{docPanel}</InFrame>
          </SheetContent>
        </Sheet>
      ) : null}
      <MembersSheet chat={chat} me={me} open={members} onOpenChange={setMembers} />
      {agentPanels ? (
        <AgentSheet
          target={agentSheet}
          panels={agentPanels}
          onTab={(tab) => setAgentSheet((current) => (current ? { ...current, tab } : current))}
          onOpenChange={(open) => (open ? undefined : setAgentSheet(null))}
        />
      ) : null}
    </div>
    </AgentActionsContext.Provider>
    </OpenDocContext.Provider>
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
