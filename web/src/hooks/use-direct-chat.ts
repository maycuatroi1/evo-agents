"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";

import { browserApi } from "@/lib/api/browser";
import { chatComposeHref, chatKeys, openDirectChat } from "@/lib/chat-keys";
import { featureOn, featuresQuery } from "@/lib/hub-settings";
import { whoamiQuery } from "@/lib/queries";

/**
 * Direct chats with agents (docs/hub.md, Group chats): what the Chats list's row of agents and the Chat buttons of the
 * Agents pages share, so that neither feature imports the other.
 */

/** The projects whose agents the visitor may add to a chat or chat with: those they write to (writer or admin); null until whoami answered. */
export function useWriterProjects(): string[] | null {
  const { data } = useQuery(whoamiQuery(browserApi));
  if (!data) return null;
  return data.grants.filter((grant) => grant.role === "writer" || grant.role === "admin").map((grant) => grant.project);
}

/** Whether the visitor may open a direct chat with an agent of `project`: chats are on, and they write to the project. */
export function useCanChat(project: string): boolean {
  const projects = useWriterProjects();
  const { data: features } = useQuery(featuresQuery(browserApi));
  return featureOn(features, "chats") && !!projects?.includes(project);
}

export type DirectChat = {
  /** Open the visitor's direct chat with agent `handle`; ignored while one is opening. */
  open: (handle: string) => void;
  /** The handle of the agent whose chat is opening, or null. */
  opening: string | null;
};

/**
 * Open the visitor's direct chat with an agent (PUT /v1/chats/direct/{handle}): the one there is, or one the hub makes
 * now, which the list then shows; then the chat's page with its composer focused. A refusal goes to `onError` with the
 * agent's handle, for the caller to say.
 */
export function useDirectChat({ onError }: { onError: (error: unknown, handle: string) => void }): DirectChat {
  const queryClient = useQueryClient();
  const router = useRouter();
  const mutation = useMutation({
    mutationFn: (handle: string) => openDirectChat(browserApi(), handle),
    onSuccess: (chat) => {
      queryClient.setQueryData(chatKeys.one(chat.id), chat);
      void queryClient.invalidateQueries({ queryKey: chatKeys.list });
      router.push(chatComposeHref(chat.id));
    },
    onError: (error, handle) => onError(error, handle),
  });
  return {
    open: (handle) => {
      if (!mutation.isPending) mutation.mutate(handle);
    },
    opening: mutation.isPending ? (mutation.variables ?? null) : null,
  };
}
