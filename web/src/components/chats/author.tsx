"use client";

import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import type { ChatAuthor } from "@/lib/chats";

/** An author's name as a message heads with it: an agent's name (its handle after it, no `@`, when the two differ), a
 * person's login, the hub. */
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
        {author.handle && author.handle.toLowerCase() !== author.name.toLowerCase() ? (
          <span className="font-mono text-xs text-muted-foreground" data-testid="message-agent-handle">
            {author.handle}
          </span>
        ) : null}
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
