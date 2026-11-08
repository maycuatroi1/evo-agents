"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import dynamic from "next/dynamic";
import { type ReactNode, useState } from "react";

import type { WriteFailure } from "@/components/feedback/notice";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { type ChatHomeAgent, openDirectChat } from "@/lib/chat-keys";
import { chatKeys, chatPageQuery, sendChatMessage } from "@/lib/chat-queries";
import { cn } from "@/lib/utils";

import { AgentPicker } from "./agent-picker";
import type { AgentPanels } from "./agent-sheet";
import { ChatStart } from "./chat-start";
import type { UseUnder } from "./chat-view";
import { useChatFailure } from "./hooks";

/**
 * The thread loads once a chat has messages: a page that starts on the agent's start screen does not carry the chat's
 * code (its list, its threads, its documents) in its first load.
 */
const ChatView = dynamic(() => import("./chat-view").then((module) => module.ChatView), { ssr: false, loading: () => <FrameLoading /> });

/**
 * Whether the frame shows the agent's start screen or its direct chat: the start while the visitor has no direct chat
 * with the agent, or one without a message; the chat once it has one, or once a message was sent from the start here.
 */
export function frameOf(chatId: number | null, opened: boolean, messages: number | undefined): "start" | "chat" | "loading" {
  if (chatId === null) return "start";
  if (opened) return "chat";
  if (messages === undefined) return "loading";
  return messages === 0 ? "start" : "chat";
}

/**
 * A chat with one agent among several (web/DESIGN.md, Chat home): the agent's start screen (its face, its composer in
 * the middle, its starters) under the agent picker's bar until the visitor's direct chat with it has a message, then
 * that chat (`ChatView`: its messages, live, its threads and their interactive answers, its composer). The chat's own
 * head names the agent, so the bar goes: the name there opens the picker, and `actions` stand before Details. The bar
 * stays while the chat cannot be read, so another agent can still be chosen.
 * The first message opens the direct chat (PUT /v1/chats/direct/{handle}) and sends; looking at an agent opens
 * nothing. The caller holds which agent is chosen (`handle`) and hears a pick (`onPick`); `useUnder` and
 * `agentPanels` go to the chat as on its own page. Home uses it, and so may any page that puts a chat with an agent in
 * a frame of its own.
 */
export function AgentChat({
  agents,
  handle,
  onPick,
  useUnder,
  agentPanels,
  actions,
  className,
}: {
  agents: readonly ChatHomeAgent[];
  handle: string;
  onPick: (handle: string) => void;
  useUnder?: UseUnder;
  agentPanels?: AgentPanels;
  actions?: ReactNode;
  className?: string;
}) {
  const queryClient = useQueryClient();
  const failure = useChatFailure();
  const agent = agents.find((candidate) => candidate.handle === handle) ?? agents[0];
  // The direct chats opened from this frame, by agent: their chat shows even before the list of agents is read again.
  const [opened, setOpened] = useState<Record<string, number>>({});
  const [error, setError] = useState<WriteFailure | null>(null);
  const chatId = opened[agent.handle] ?? agent.chat_id;
  const page = useQuery({ ...chatPageQuery(browserApi, chatId ?? 0), enabled: chatId !== null && !(agent.handle in opened) });

  const send = useMutation({
    mutationFn: async ({ to, body }: { to: ChatHomeAgent; body: string }) => {
      const api = browserApi();
      let id = opened[to.handle] ?? to.chat_id;
      if (id === null) {
        const chat = await openDirectChat(api, to.handle);
        queryClient.setQueryData(chatKeys.one(chat.id), chat);
        id = chat.id;
      }
      await sendChatMessage(api, id, body, null);
      // The chat's page is read once and kept: read it again so the chat opens with the message just sent.
      await queryClient.fetchQuery({ ...chatPageQuery(browserApi, id), staleTime: 0 });
      return { handle: to.handle, id };
    },
    onMutate: () => setError(null),
    onError: (cause) => setError(failure(cause)),
    onSuccess: ({ handle: sentTo, id }) => {
      setOpened((current) => ({ ...current, [sentTo]: id }));
      void queryClient.invalidateQueries({ queryKey: chatKeys.agents });
      void queryClient.invalidateQueries({ queryKey: chatKeys.list });
    },
  });

  const frame = frameOf(chatId, agent.handle in opened, page.data?.messages.length ?? (page.isError ? 1 : undefined));
  const choose = (next: string) => {
    setError(null);
    onPick(next);
  };
  // The chat's head names the agent: the bar over it would say it twice.
  const bar = frame === "start" || page.isError;

  return (
    <div className={cn("flex min-h-0 w-full flex-col gap-3", className)} data-testid="agent-chat" data-agent={agent.handle} data-frame={frame}>
      {bar ? (
        <div className="flex min-w-0 items-center gap-2">
          <AgentPicker agents={agents} current={agent} onPick={choose} className="min-w-0 shrink" />
          {actions ? <div className="ml-auto flex shrink-0 items-center gap-2">{actions}</div> : null}
        </div>
      ) : null}
      <div className="flex min-h-0 flex-1">
        {frame === "start" ? (
          <ChatStart
            key={agent.handle}
            agent={agent}
            onSend={(body) => send.mutate({ to: agent, body })}
            pending={send.isPending}
            error={error}
          />
        ) : frame === "chat" && chatId !== null ? (
          <ChatView
            key={chatId}
            chatId={chatId}
            initialError={null}
            useUnder={useUnder}
            agentPanels={agentPanels}
            headSlots={bar ? undefined : { title: (name) => <AgentPicker agents={agents} current={agent} onPick={choose} title={name} />, actions }}
          />
        ) : (
          <FrameLoading />
        )}
      </div>
    </div>
  );
}

function FrameLoading() {
  return (
    <div className="flex h-full w-full flex-col overflow-hidden rounded-md border bg-card shadow-raised" data-testid="agent-chat-loading">
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
