"use client";

import { Users } from "lucide-react";

import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import type { ChatAuthor, ChatFace } from "@/lib/chats";
import { cn } from "@/lib/utils";

import { AuthorAvatar } from "./faces";

type Size = "default" | "lg";

/** The round of the face (32 or 40 px) and, for two members, each one's smaller round inside it. */
const LOOK: Record<Size, { box: string; pair: string; icon: string }> = {
  default: { box: "size-8", pair: "size-5.5", icon: "size-4" },
  lg: { box: "size-10", pair: "size-7", icon: "size-5" },
};

function Members({ faces, size }: { faces: readonly ChatAuthor[]; size: Size }) {
  const look = LOOK[size];
  if (faces.length === 1) return <AuthorAvatar author={faces[0]} size={size} />;
  if (faces.length === 0) {
    return (
      <span className={cn("grid shrink-0 place-items-center rounded-full border bg-muted text-muted-foreground", look.box)} data-face-part="none">
        <Users className={look.icon} aria-hidden="true" />
      </span>
    );
  }
  // Two members as Messenger draws a group: one up on the right, the other down on the left over it, cut out by the
  // card's colour so the two read apart.
  return (
    <span className={cn("relative block shrink-0", look.box)} data-face-part="pair">
      <AuthorAvatar author={faces[0]} size="sm" className={cn("absolute top-0 right-0", look.pair)} />
      <AuthorAvatar author={faces[1]} size="sm" className={cn("absolute bottom-0 left-0 ring-2 ring-card", look.pair)} />
    </span>
  );
}

/**
 * A chat's face (web/DESIGN.md, Chats), round: a direct chat's agent (its avatar, or its initial on its colour); a
 * group's uploaded image, its members drawn until it loads and when it fails; else two of its other members, or the
 * one other member, or a neutral round with the people icon for a group of the visitor alone. Decorative: the chat's
 * name stands beside it.
 */
export function ChatAvatar({ face, size = "default", className }: { face: ChatFace; size?: Size; className?: string }) {
  const look = LOOK[size];
  let body;
  if (face.kind === "agent") body = <AuthorAvatar author={face.agent} size={size} />;
  else if (face.kind === "image") {
    body = (
      <Avatar className={look.box}>
        <AvatarImage src={face.url} alt="" data-testid="chat-image" />
        <AvatarFallback className="bg-transparent">
          <Members faces={face.faces} size={size} />
        </AvatarFallback>
      </Avatar>
    );
  } else body = <Members faces={face.kind === "members" ? face.faces : []} size={size} />;
  return (
    <span aria-hidden="true" className={cn("inline-flex shrink-0", className)} data-testid="chat-avatar" data-face={face.kind}>
      {body}
    </span>
  );
}
