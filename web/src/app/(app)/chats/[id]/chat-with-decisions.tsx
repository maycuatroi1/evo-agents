"use client";

import { useQuery } from "@tanstack/react-query";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";

import { ChatView } from "@/components/chats/chat-view";
import { useMe } from "@/components/chats/hooks";
import { workHeadSlots } from "@/components/chats/work-head";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { chatPageQuery } from "@/lib/chat-queries";
import { workCardQuery } from "@/lib/work";

import { AGENT_PANELS } from "./agent-panels";
import { useChatDecisions } from "./use-chat-decisions";
import { useWorkUnder, WorkChatContext } from "./use-work-under";
import { WorkDecisionAnswer } from "./work-decision";

/** The pinned work card loads with the first work chat opened. */
const WorkPinned = dynamic(() => import("@/components/chats/work-card").then((module) => module.WorkPinned), { ssr: false });

/**
 * The chat, with the decisions its agents' runs asked shown under the message that ended their turn, and the sheet an
 * agent's menu opens on its workers and pools or its accounts. A work chat (kind `work`) has its work card too
 * (web/DESIGN.md, Work card): its state, number, system and coordinator in the head, the card pinned under it with the
 * visitor's open decisions answered there, the cards of its sub-tasks and exceptions under the messages they are
 * about, and a composer that says a message goes to the coordinator unless it mentions an agent.
 */
export function ChatWithDecisions({ chatId, initialError }: { chatId: number; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("chats.work");
  const me = useMe();
  // The page the chat reads: shared with ChatView, so this reads nothing more.
  const { data: page } = useQuery({ ...chatPageQuery(browserApi, chatId), enabled: false });
  const chat = page?.chat ?? null;
  const isWork = chat?.kind === "work";
  const work = useQuery({ ...workCardQuery(browserApi, chatId), enabled: isWork });
  // A chat known to be a work chat only once its page came draws its chat anew, since what goes under its messages
  // reads more then.
  if (!chat || !isWork) {
    return <ChatView key="chat" chatId={chatId} initialError={initialError} useUnder={useChatDecisions} agentPanels={AGENT_PANELS} />;
  }
  const card = work.data ?? null;
  const project = card?.system.project ?? null;
  const state = card
    ? ({ status: "success", card } as const)
    : work.error
      ? ({ status: "error", error: work.error.info, retry: () => void work.refetch() } as const)
      : ({ status: "pending" } as const);
  return (
    <WorkChatContext.Provider value={{ chat, card }}>
      <ChatView
        key="work"
        chatId={chatId}
        initialError={initialError}
        useUnder={useWorkUnder}
        agentPanels={AGENT_PANELS}
        headSlots={workHeadSlots(card)}
        slots={{
          pinned: (
            <WorkPinned
              state={state}
              chatId={chatId}
              me={me}
              answer={project ? (decision) => <WorkDecisionAnswer chatId={chatId} project={project} id={decision.id} /> : undefined}
            />
          ),
          placeholder: card?.coordinator ? t("placeholder", { coordinator: card.coordinator }) : t("placeholderAny"),
        }}
      />
    </WorkChatContext.Provider>
  );
}
