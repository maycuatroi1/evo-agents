"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Camera, ImageUp, LoaderCircle, Trash2 } from "lucide-react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { useRef, useState } from "react";

import type { WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { browserApi } from "@/lib/api/browser";
import { AVATAR_MAX_BYTES, AVATAR_TYPES, avatarProblem, fileBase64 } from "@/lib/avatars";
import { chatKeys, removeChatImage, setChatImage } from "@/lib/chat-queries";
import { type Chat, chatFace } from "@/lib/chats";

import { ChatAvatar } from "./chat-avatar";
import { useChatFailure } from "./hooks";

// The confirmation loads when Remove image is first chosen: the chat's head carries only its menu.
const ConfirmAction = dynamic(() => import("@/components/feedback/confirm-action").then((module) => module.ConfirmAction), { ssr: false });

/** A problem of the image picked, said before anything is sent, in the shape of a refusal. */
function pickProblem(text: string): WriteFailure {
  return { text, detail: null, requestId: null, status: 0 };
}

/**
 * A group chat's face in its head, as a button any member presses to change it (web/DESIGN.md, Chats): a menu with
 * Upload image or Change image (a PNG, JPEG or WebP image of at most 512 KiB, which the menu says) and, once there is
 * one, Remove image after a confirmation. A file of another type or over the size is refused before it is sent;
 * `onProblem` says that, or the hub's refusal, under the head, and clears it on the next try. A change made is a toast,
 * and the chat's stream shows it to the other members.
 */
export function ChatImageControl({ chat, me, onProblem }: { chat: Chat; me: string | null; onProblem: (problem: WriteFailure | null) => void }) {
  const t = useTranslations("chats.image");
  const queryClient = useQueryClient();
  const failure = useChatFailure();
  const input = useRef<HTMLInputElement>(null);
  const [removing, setRemoving] = useState(false);

  const write = useMutation({
    mutationFn: async (file: File | null): Promise<Chat> => {
      const api = browserApi();
      return file ? setChatImage(api, chat.id, await fileBase64(file)) : removeChatImage(api, chat.id);
    },
    onMutate: () => onProblem(null),
    onSuccess: (updated, file) => {
      queryClient.setQueryData(chatKeys.one(chat.id), updated);
      void queryClient.invalidateQueries({ queryKey: chatKeys.list });
      setRemoving(false);
      notify({ tone: "success", text: file ? t("changed") : t("removed") });
    },
    // A failed removal shows in its confirmation, which stays open.
    onError: (error, file) => (file ? onProblem(failure(error)) : undefined),
  });

  const pick = (file: File | undefined) => {
    if (!file) return;
    const problem = avatarProblem(file);
    if (problem) return onProblem(pickProblem(problem === "type" ? t("type") : t("size", { max: AVATAR_MAX_BYTES / 1024 })));
    write.mutate(file);
  };

  const hasImage = chat.image_url !== null;
  return (
    <>
      <input
        ref={input}
        type="file"
        accept={AVATAR_TYPES.join(",")}
        className="sr-only"
        tabIndex={-1}
        aria-hidden="true"
        onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = ""; // the same file picked again is a new pick
          pick(file);
        }}
        data-testid="chat-image-input"
      />
      <DropdownMenu modal={false}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            aria-label={hasImage ? t("menuWithImage") : t("menu")}
            aria-busy={write.isPending || undefined}
            className="group/face relative shrink-0 cursor-pointer rounded-full aria-busy:cursor-progress"
            data-testid="chat-image-button"
          >
            <ChatAvatar face={chatFace(chat, me)} size="lg" />
            <span
              aria-hidden="true"
              className="absolute -right-1 -bottom-1 grid size-5 place-items-center rounded-full border bg-card text-muted-foreground shadow-raised transition-colors group-hover/face:text-foreground"
            >
              <Camera className="size-3" />
            </span>
            {write.isPending ? (
              <span className="absolute inset-0 grid place-items-center rounded-full bg-background/70" data-testid="chat-image-busy">
                <LoaderCircle className="size-5 animate-spin text-running motion-reduce:animate-none" aria-hidden="true" />
              </span>
            ) : null}
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-64" data-testid="chat-image-menu">
          <DropdownMenuLabel className="text-xs font-normal text-pretty text-muted-foreground">{t("hint", { max: AVATAR_MAX_BYTES / 1024 })}</DropdownMenuLabel>
          <DropdownMenuItem disabled={write.isPending} onSelect={() => input.current?.click()} data-testid="chat-image-upload">
            <ImageUp aria-hidden="true" />
            {hasImage ? t("change") : t("upload")}
          </DropdownMenuItem>
          {hasImage ? (
            <DropdownMenuItem variant="destructive" disabled={write.isPending} onSelect={() => setRemoving(true)} data-testid="chat-image-remove">
              <Trash2 aria-hidden="true" />
              {t("remove")}
            </DropdownMenuItem>
          ) : null}
        </DropdownMenuContent>
      </DropdownMenu>
      {removing ? (
        <ConfirmAction
          open={removing}
          onOpenChange={(open) => {
            if (open) write.reset();
            setRemoving(open);
          }}
          title={t("removeTitle", { title: chat.title })}
          description={<p>{t("removeText")}</p>}
          confirmLabel={t("removeConfirm")}
          pendingLabel={t("removing")}
          cancelLabel={t("cancel")}
          pending={write.isPending}
          error={write.isError && write.variables === null ? failure(write.error) : null}
          onConfirm={() => write.mutate(null)}
          testId="chat-image-remove-confirm"
        />
      ) : null}
    </>
  );
}
