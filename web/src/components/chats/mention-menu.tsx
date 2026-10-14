"use client";

import { useTranslations } from "next-intl";

import { AgentAvatar } from "@/components/data/agent-avatar";
import type { MentionTarget } from "@/lib/chats";
import { cn } from "@/lib/utils";

import { AuthorAvatar } from "./author";

const GROUPS = ["agent", "project", "person"] as const;

export function optionId(base: string, index: number): string {
  return `${base}-option-${index}`;
}

/**
 * The suggestions while a name is typed after `@` in the composer (web/DESIGN.md, Chats): the chat's agents by handle
 * and name, its projects (the default agent each names), its people. A listbox the text area drives
 * (`aria-activedescendant`): the arrow keys move, Enter or Tab puts the name in, Escape closes it; a click puts it in
 * without taking the focus from the text.
 */
export function MentionMenu({
  id,
  options,
  active,
  onPick,
  onHover,
}: {
  id: string;
  options: readonly MentionTarget[];
  active: number;
  onPick: (target: MentionTarget) => void;
  onHover: (index: number) => void;
}) {
  const t = useTranslations("chats.composer.mention");
  return (
    <div
      id={id}
      role="listbox"
      aria-label={t("label")}
      className="absolute bottom-full left-0 z-50 mb-1.5 max-h-72 w-full max-w-sm overflow-y-auto rounded-md border bg-popover p-1 text-popover-foreground shadow-popover"
      data-testid="mention-menu"
    >
      {GROUPS.map((kind) => {
        const members = options.map((target, index) => ({ target, index })).filter((item) => item.target.kind === kind);
        if (!members.length) return null;
        return (
          <div key={kind} role="group" aria-labelledby={`${id}-${kind}`}>
            <div id={`${id}-${kind}`} className="px-2 pt-1.5 pb-1 text-[11px] leading-4 font-semibold tracking-[0.06em] text-fg-subtle uppercase">
              {t(`groups.${kind}`)}
            </div>
            {members.map(({ target, index }) => (
              <div
                key={`${target.kind}:${target.name}`}
                id={optionId(id, index)}
                role="option"
                aria-selected={index === active}
                onMouseDown={(event) => event.preventDefault()}
                onMouseMove={() => onHover(index)}
                onClick={() => onPick(target)}
                data-testid="mention-option"
                data-name={target.name}
                data-kind={target.kind}
                className={cn(
                  "flex cursor-pointer items-center gap-2.5 rounded-sm px-2 py-1.5 text-sm max-md:min-h-11",
                  index === active ? "bg-surface-selected" : "hover:bg-accent",
                )}
              >
                {target.kind === "person" ? (
                  <AuthorAvatar author={{ kind: "person", login: target.name, name: target.name }} size="sm" />
                ) : (
                  <AgentAvatar agent={target.agent} size="sm" />
                )}
                <span className="min-w-0 flex-1 truncate">
                  <span className="font-medium text-foreground">@{target.name}</span>
                  {target.kind !== "person" ? (
                    <span className="ml-2 text-xs text-muted-foreground">
                      {target.kind === "project" ? t("project", { project: target.label }) : target.label}
                    </span>
                  ) : null}
                </span>
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}
