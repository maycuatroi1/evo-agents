"use client";

import { SmilePlus } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { ChatReaction } from "@/lib/chats";
import { cn } from "@/lib/utils";

import { emojiName, PICKER } from "./emoji";

/** How many names a reaction's label lists before it says how many more. */
const NAMES_SHOWN = 3;

/**
 * Who put a reaction, as its label says it: the visitor first as "you", then people and agents (@handle), the first
 * three and how many more.
 */
export function useReactionLabel() {
  const t = useTranslations("chats.reactions");
  const tEmoji = useTranslations("chats.emoji");
  const format = useFormatter();
  return (reaction: ChatReaction, me: string | null) => {
    const people = reaction.people.filter((login) => login.toLowerCase() !== me?.toLowerCase());
    const names = [...(reaction.mine ? [t("you")] : []), ...people, ...reaction.agents.map((handle) => `@${handle}`)];
    const shown = names.slice(0, NAMES_SHOWN);
    const more = Math.max(reaction.count - shown.length, 0);
    const who = more > 0 ? t("andMore", { names: format.list(shown, { type: "conjunction" }), count: more }) : format.list(shown, { type: "conjunction" });
    const key = emojiName(reaction.emoji);
    const name = key ? tEmoji(key) : reaction.emoji;
    return t("label", { name, count: reaction.count, who });
  };
}

/**
 * A message's reactions (web/DESIGN.md, Style: the one place for emoji): a round chip per emoji with its count, the
 * visitor's own pressed; pressing one puts the visitor's emoji or takes it off. Its accessible name says the emoji's
 * name, the count and who put it. The status reactions of an agent's run are chips like any other.
 */
export function ReactionChips({
  reactions,
  me,
  busy,
  onToggle,
}: {
  reactions: readonly ChatReaction[];
  me: string | null;
  busy: boolean;
  onToggle: (emoji: string, on: boolean) => void;
}) {
  const label = useReactionLabel();
  if (!reactions.length) return null;
  return (
    <ul className="flex flex-wrap items-center gap-1.5" data-testid="message-reactions">
      {reactions.map((reaction) => {
        const text = label(reaction, me);
        return (
          <li key={reaction.emoji}>
            <button
              type="button"
              aria-pressed={reaction.mine}
              aria-label={text}
              title={text}
              aria-disabled={busy || undefined}
              onClick={() => (busy ? undefined : onToggle(reaction.emoji, !reaction.mine))}
              data-testid="reaction-chip"
              data-emoji={reaction.emoji}
              data-count={reaction.count}
              data-mine={reaction.mine}
              className={cn(
                "inline-flex h-6 cursor-pointer items-center gap-1 rounded-full border px-2 text-xs font-medium tabular-nums transition-colors max-md:h-8",
                reaction.mine
                  ? "border-brand/40 bg-brand-soft text-brand hover:bg-surface-selected"
                  : "border-border bg-card text-muted-foreground hover:bg-accent hover:text-foreground",
              )}
            >
              <span aria-hidden="true" className="text-[13px] leading-none">
                {reaction.emoji}
              </span>
              <span aria-hidden="true">{reaction.count}</span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * Add reaction: a ghost icon button that opens the picker, a grid of the emoji the hub's chats use most, each named.
 * Picking one the visitor put already takes it off.
 */
export function ReactionPicker({
  reactions,
  busy,
  onToggle,
  className,
}: {
  reactions: readonly ChatReaction[];
  busy: boolean;
  onToggle: (emoji: string, on: boolean) => void;
  className?: string;
}) {
  const t = useTranslations("chats.reactions");
  const tEmoji = useTranslations("chats.emoji");
  const [open, setOpen] = useState(false);
  const mine = new Set(reactions.filter((reaction) => reaction.mine).map((reaction) => reaction.emoji));
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button type="button" variant="ghost" size="icon-sm" aria-label={t("add")} title={t("add")} className={className} data-testid="reaction-add">
          <SmilePlus aria-hidden="true" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-auto p-2" data-testid="reaction-picker">
        <div role="group" aria-label={t("pick")} className="grid grid-cols-8 gap-1 max-sm:grid-cols-6">
          {PICKER.map((item) => {
            const on = mine.has(item.emoji);
            const name = tEmoji(item.name);
            return (
              <button
                key={item.emoji}
                type="button"
                aria-label={on ? t("takeOff", { name }) : name}
                aria-pressed={on}
                title={name}
                disabled={busy}
                onClick={() => {
                  setOpen(false);
                  onToggle(item.emoji, !on);
                }}
                data-testid="reaction-option"
                data-emoji={item.emoji}
                className={cn(
                  "grid size-8 cursor-pointer place-items-center rounded-sm text-base transition-colors hover:bg-accent disabled:cursor-not-allowed disabled:opacity-60 max-md:size-11",
                  on && "bg-surface-selected",
                )}
              >
                <span aria-hidden="true">{item.emoji}</span>
              </button>
            );
          })}
        </div>
      </PopoverContent>
    </Popover>
  );
}
