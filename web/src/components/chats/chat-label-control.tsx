"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, ShieldCheck } from "lucide-react";
import { useTranslations } from "next-intl";

import type { WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuLabel, DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { browserApi } from "@/lib/api/browser";
import { chatKeys, removeChatLabel, setChatLabel } from "@/lib/chat-queries";
import type { Chat } from "@/lib/chats";

import { useChatFailure } from "./hooks";

/** The radio value of the projects' default, which no level of a ladder can be (a level starts with a letter or digit). */
const DEFAULT = "";

/**
 * The level of a group chat's label, in its head (web/DESIGN.md, Chats), for an admin of every project of its agents
 * (`chat.label_admin`): a menu of the levels the hub says it may be set to (`chat.label_levels`, lowest first, at or
 * below each project's default) and the projects' default. A choice is sent at once; the hub's answer is the chat as it
 * is now, and a refusal (someone in the chat would no longer clear the label, say) is said under the head through
 * `onProblem`. A message keeps the level it was sent at; only those sent after the change carry the new one. Nothing
 * is drawn for anyone else, a direct chat, or a chat without agents.
 */
export function ChatLabelControl({ chat, onProblem }: { chat: Chat; onProblem: (problem: WriteFailure | null) => void }) {
  const t = useTranslations("chats.labelLevel");
  const queryClient = useQueryClient();
  const failure = useChatFailure();
  const levels = chat.label_levels ?? [];

  const write = useMutation({
    mutationFn: (level: string): Promise<Chat> => (level === DEFAULT ? removeChatLabel(browserApi(), chat.id) : setChatLabel(browserApi(), chat.id, level)),
    onMutate: () => onProblem(null),
    onSuccess: (updated, level) => {
      queryClient.setQueryData(chatKeys.one(chat.id), updated);
      void queryClient.invalidateQueries({ queryKey: chatKeys.list });
      notify({ tone: "success", text: level === DEFAULT ? t("removed") : t("changed", { level }) });
    },
    onError: (error) => onProblem(failure(error)),
  });

  if (chat.kind === "direct" || !chat.label_admin || levels.length === 0) return null;
  const current = chat.label_level ?? DEFAULT;
  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <Button type="button" variant="outline" size="sm" aria-busy={write.isPending || undefined} data-testid="chat-label-open">
          {write.isPending ? <LoaderCircle className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <ShieldCheck aria-hidden="true" />}
          <span>{t("menu", { level: chat.label_level ?? t("defaultLevel") })}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-72" data-testid="chat-label-menu">
        <DropdownMenuLabel className="text-xs font-normal text-pretty text-muted-foreground">{t("hint")}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuRadioGroup value={current} onValueChange={(level) => (level === current ? undefined : write.mutate(level))}>
          <DropdownMenuRadioItem value={DEFAULT} disabled={write.isPending} data-testid="chat-label-default">
            {t("default")}
          </DropdownMenuRadioItem>
          {levels.map((level) => (
            <DropdownMenuRadioItem key={level} value={level} disabled={write.isPending} data-testid="chat-label-level">
              {level}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
