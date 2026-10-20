"use client";

import { BrandMark } from "@/components/brand";
import { AgentAvatar } from "@/components/data/agent-avatar";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import type { ChatAuthor } from "@/lib/chats";
import { cn, initials } from "@/lib/utils";

/** The colour an agent goes by when the hub sends none (never for an agent it knows): neutral-solid. */
const NO_COLOUR = "#6b7383";

export type FaceSize = "sm" | "default" | "lg";

const BOX: Record<FaceSize, string> = { sm: "size-6", default: "size-8", lg: "size-10" };
const LETTERS: Record<FaceSize, string> = { sm: "text-[10px]", default: "text-xs", lg: "text-sm" };

/** What a face is drawn from: who it is, for an agent its colour and avatar, for a person their GitHub avatar. */
export type Face = Pick<ChatAuthor, "kind" | "name"> & Partial<Pick<ChatAuthor, "login" | "handle" | "color" | "icon_url" | "avatar_url">>;

/**
 * A person's face: their GitHub avatar, or until it loads (and when there is none or it fails) their initials on a
 * neutral round. Decorative beside the name; `label` names it where it stands alone.
 */
export function PersonAvatar({ login, src, size = "default", label, className }: { login: string; src?: string | null; size?: FaceSize; label?: string; className?: string }) {
  return (
    <Avatar
      className={cn(BOX[size], className)}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      data-testid="person-avatar"
      data-avatar={src ? "image" : "initial"}
    >
      {src ? <AvatarImage src={src} alt="" /> : null}
      <AvatarFallback className={cn("font-medium select-none", LETTERS[size])}>{initials(login)}</AvatarFallback>
    </Avatar>
  );
}

/**
 * Who wrote a message or put a reaction, as a face (web/DESIGN.md, Chats): an agent's avatar or its initial on its
 * colour, a person's GitHub avatar or initials on a neutral round, the hub's mark. Decorative beside the name.
 */
export function AuthorAvatar({ author, size = "default", className }: { author: Face; size?: FaceSize; className?: string }) {
  if (author.kind === "agent") {
    return (
      <AgentAvatar
        agent={{ name: author.name, handle: author.handle ?? author.name, color: author.color ?? NO_COLOUR, icon_url: author.icon_url ?? null }}
        size={size}
        className={className}
      />
    );
  }
  if (author.kind === "hub") return <BrandMark className={cn(BOX[size], className)} />;
  return <PersonAvatar login={author.login ?? author.name} src={author.avatar_url} size={size} className={className} />;
}
