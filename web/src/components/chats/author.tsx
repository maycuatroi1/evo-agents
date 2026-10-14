"use client";

import { useTranslations } from "next-intl";

import { BrandMark } from "@/components/brand";
import { AgentAvatar } from "@/components/data/agent-avatar";
import { Tag } from "@/components/data/identifier";
import type { ChatAuthor } from "@/lib/chats";
import { cn, initials } from "@/lib/utils";

/** The colour an agent goes by when the hub sends none (never for an agent it knows): neutral-solid. */
const NO_COLOUR = "#6b7383";

type Size = "sm" | "default";

/** What a face is drawn from: who it is, and for an agent its colour and avatar. */
export type Face = Pick<ChatAuthor, "kind" | "name"> & Partial<Pick<ChatAuthor, "login" | "handle" | "color" | "icon_url">>;

/**
 * Who wrote a message or put a reaction, as a face (web/DESIGN.md, Chats): an agent's avatar or its initial on its
 * colour, a person's initials on a neutral round, the hub's mark. Decorative beside the name.
 */
export function AuthorAvatar({ author, size = "default", className }: { author: Face; size?: Size; className?: string }) {
  if (author.kind === "agent") {
    return (
      <AgentAvatar
        agent={{ name: author.name, handle: author.handle ?? author.name, color: author.color ?? NO_COLOUR, icon_url: author.icon_url ?? null }}
        size={size}
        className={className}
      />
    );
  }
  const box = size === "sm" ? "size-6" : "size-8";
  if (author.kind === "hub") return <BrandMark className={cn(box, className)} />;
  return (
    <span
      aria-hidden="true"
      data-testid="person-avatar"
      className={cn(
        "grid shrink-0 place-items-center rounded-full border bg-muted font-medium text-muted-foreground select-none",
        box,
        size === "sm" ? "text-[10px]" : "text-xs",
        className,
      )}
    >
      {initials(author.login ?? author.name)}
    </span>
  );
}

/** An author's name as a message heads with it: an agent's name and @handle, a person's login, the hub. */
export function AuthorName({ author, me }: { author: ChatAuthor; me: string | null }) {
  const t = useTranslations("chats.message");
  if (author.kind === "hub") {
    return (
      <span className="inline-flex items-center gap-1.5">
        <span className="text-sm font-semibold text-foreground">{t("hub")}</span>
        <Tag data-testid="message-hub-tag">{t("hubTag")}</Tag>
      </span>
    );
  }
  if (author.kind === "agent") {
    return (
      <span className="inline-flex min-w-0 flex-wrap items-baseline gap-x-1.5">
        <span className="text-sm font-semibold text-foreground">{author.name}</span>
        <span className="font-mono text-xs text-muted-foreground">@{author.handle}</span>
        {author.deleted ? <Tag data-testid="message-agent-deleted">{t("deleted")}</Tag> : null}
      </span>
    );
  }
  const mine = !!me && author.login?.toLowerCase() === me.toLowerCase();
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className="text-sm font-semibold text-foreground">{author.login ?? author.name}</span>
      {mine ? <span className="text-xs text-fg-subtle">{t("you")}</span> : null}
    </span>
  );
}
