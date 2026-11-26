"use client";

import { useQuery } from "@tanstack/react-query";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";

import { type ChatLineProps, ChatView } from "@/components/chats/chat-view";
import { browserApi } from "@/lib/api/browser";
import { chatPageQuery } from "@/lib/chat-queries";
import { workCardQuery } from "@/lib/work";

import { AGENT_PANELS } from "./chats/[id]/agent-panels";
import { ChatWithDecisions } from "./chats/[id]/chat-with-decisions";
import { useWorkUnder, WorkChatContext } from "./chats/[id]/use-work-under";

/** The task view loads with the first task opened on Home. */
const TaskLine = dynamic(() => import("@/components/chats/task-view").then((module) => module.TaskLine), { ssr: false });
/** The work card's loading and error states, while the task's card is not read yet. */
const WorkPinned = dynamic(() => import("@/components/chats/work-card").then((module) => module.WorkPinned), { ssr: false });
const WorkDecisionAnswer = dynamic(() => import("./chats/[id]/work-decision").then((module) => module.WorkDecisionAnswer), { ssr: false });

/**
 * A task opened in Home's main pane (web/DESIGN.md, Chat home, The task): a work chat draws as the task view
 * (`TaskLine`: its source line, title, state, figures, goal, team, then its conversation folded into hand-offs), with
 * the decisions of its agents' runs under their messages and the visitor's open decisions answered in its head. Any
 * other chat of the rail (a group chat) draws as on its own page.
 */
export function HomeTask({ chatId }: { chatId: number }) {
  const t = useTranslations("chats.work");
  // The page the chat reads: shared with ChatView, so this reads nothing more.
  const { data: page } = useQuery({ ...chatPageQuery(browserApi, chatId), enabled: false });
  const chat = page?.chat ?? null;
  const isWork = chat?.kind === "work";
  const work = useQuery({ ...workCardQuery(browserApi, chatId), enabled: isWork });
  if (!chat || !isWork) return <ChatWithDecisions key="chat" chatId={chatId} initialError={null} />;
  const card = work.data ?? null;
  return (
    <WorkChatContext.Provider value={{ chat, card }}>
      <ChatView
        key="task"
        chatId={chatId}
        initialError={null}
        useUnder={useWorkUnder}
        agentPanels={AGENT_PANELS}
        slots={{
          line: HomeTaskLine,
          placeholder: card?.coordinator ? t("placeholder", { coordinator: card.coordinator }) : t("placeholderAny"),
        }}
      />
    </WorkChatContext.Provider>
  );
}

/** The task's head and conversation, once its card is read; till then the work card's loading or its error. */
function HomeTaskLine(props: ChatLineProps) {
  const chatId = props.chat.id;
  // The card HomeTask reads: shared, so this reads nothing more.
  const work = useQuery(workCardQuery(browserApi, chatId));
  const card = work.data ?? null;
  const project = card?.system.project ?? null;
  if (!card) {
    return (
      <WorkPinned
        state={work.error ? { status: "error", error: work.error.info, retry: () => void work.refetch() } : { status: "pending" }}
        chatId={chatId}
        me={props.me}
      />
    );
  }
  return <TaskLine card={card} answer={project ? (decision) => <WorkDecisionAnswer chatId={chatId} project={project} id={decision.id} /> : undefined} {...props} />;
}
