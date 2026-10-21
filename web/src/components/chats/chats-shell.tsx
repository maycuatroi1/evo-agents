"use client";

import { MessagesSquare } from "lucide-react";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { EmptyState } from "@/components/states/states";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { cn } from "@/lib/utils";

import { ChatList } from "./chat-list";

/** The chat a path shows (`/chats/12`), or null for the list alone. */
export function chatIdOf(pathname: string): number | null {
  const found = /^\/chats\/([1-9][0-9]{0,17})(?:\/|$)/.exec(pathname);
  return found ? Number(found[1]) : null;
}

/**
 * The Chats area (web/DESIGN.md, Chats), the same frame for the list and for a chat, so the list keeps its place as the
 * visitor moves between chats. From 1024 px one window tall: the list in a column of 19 rem (21 from 2xl) and the chat
 * beside it, each scrolling on its own. Below, the list is the page at /chats and a chat is the page at /chats/{id},
 * as tall as the window, with a link back to the list.
 */
export function ChatsShell({ initialError, children }: { initialError: ApiErrorInfo | null; children: ReactNode }) {
  const activeId = chatIdOf(usePathname());
  const open = activeId !== null;
  return (
    <div
      className={cn(
        "grid min-w-0 gap-4 lg:h-[calc(100dvh-6.25rem)] lg:min-h-[32rem] lg:grid-cols-[19rem_minmax(0,1fr)] 2xl:grid-cols-[21rem_minmax(0,1fr)]",
        open && "h-[calc(100dvh-5.25rem)] min-h-[28rem] md:h-[calc(100dvh-6.25rem)]",
      )}
      data-testid="chats"
      data-chat-open={open}
    >
      <ChatList activeId={activeId} initialError={initialError} className={open ? "hidden lg:flex" : "flex"} />
      <div className={cn("min-h-0 min-w-0", open ? "flex" : "hidden lg:flex")}>{children}</div>
    </div>
  );
}

/** The pane beside the list when no chat is open, from 1024 px: what to do next. */
export function NoChatOpen() {
  const t = useTranslations("chats.list");
  return (
    <div className="flex w-full items-center justify-center rounded-md border border-dashed bg-card/40" data-testid="chats-none-open">
      <EmptyState icon={MessagesSquare} title={t("noneOpenTitle")} description={t("noneOpenBody")} className="border-0 bg-transparent shadow-none" />
    </div>
  );
}
