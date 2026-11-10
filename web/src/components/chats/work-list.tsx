"use client";

import { ChevronRight, Network } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import type { ChatSummary } from "@/lib/chats";
import type { WorkGroup } from "@/lib/work";

import { ChatRow } from "./chat-row";

/**
 * The work chats of one agent system in the chat list (web/DESIGN.md, Work card): the system's name over its works,
 * running, waiting for a person or failed first, each row with its state's dot and what it waits for, then the done
 * ones folded under "N done", unfolded while the chat shown is one of them.
 */
export function WorkGroupRows({ group, activeId, me }: { group: WorkGroup; activeId: number | null; me: string | null }) {
  const t = useTranslations("chats.work.list");
  const ids = useId();
  const [open, setOpen] = useState(() => group.done.some((chat) => chat.id === activeId));
  const rows = (chats: ChatSummary[]) => chats.map((chat) => <ChatRow key={chat.id} chat={chat} active={chat.id === activeId} me={me} />);
  return (
    <div className="flex flex-col pt-2" data-testid="work-group" data-system={group.key}>
      <p id={`${ids}-name`} className="flex min-w-0 items-center gap-1.5 px-2.5 pb-1 text-xs font-medium text-muted-foreground" data-testid="work-group-name">
        <Network className="size-3.5 shrink-0" aria-hidden="true" />
        <span className="truncate">{t("group", { name: group.name })}</span>
      </p>
      {group.open.length ? (
        <ul className="flex flex-col" aria-labelledby={`${ids}-name`}>
          {rows(group.open)}
        </ul>
      ) : null}
      {group.done.length ? (
        <details open={open} onToggle={(event) => setOpen(event.currentTarget.open)} className="group/done" data-testid="work-done-group">
          <summary className="flex min-h-9 cursor-pointer list-none items-center gap-1.5 rounded-sm px-2.5 text-xs font-medium text-muted-foreground hover:bg-accent [&::-webkit-details-marker]:hidden">
            <ChevronRight className="size-3.5 shrink-0 transition-transform group-open/done:rotate-90" aria-hidden="true" />
            {t("done", { count: group.done.length })}
          </summary>
          <ul className="flex flex-col" aria-label={t("doneLabel", { name: group.name })}>
            {rows(group.done)}
          </ul>
        </details>
      ) : null}
    </div>
  );
}
