"use client";

import { FolderKanban, LoaderCircle, type LucideIcon, TriangleAlert, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { type KeyboardEvent, type ReactNode, useRef, useState } from "react";

import { type ShortcutId, ShortcutKeys } from "@/components/shell/shortcuts";
import { Command, CommandGroup, CommandInput, CommandItem, CommandList, CommandShortcut } from "@/components/ui/command";
import { DialogClose } from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/kbd";

import type { Scope } from "./model";

export type GroupKey = "actions" | "runs" | "plans" | "workers" | "goto" | "help";

export type Entry<T> = {
  /** cmdk's value: unique in the palette, stable while the item stays. */
  value: string;
  icon: LucideIcon;
  label: ReactNode;
  /** What a query is matched against. */
  text: string;
  meta?: string;
  /** The key that does the same from the page (G P, D), shown at the item's end. */
  shortcut?: ShortcutId;
  target: T;
};

/** The keys with which cmdk moves the selection. */
const MOVE_KEYS = new Set(["ArrowDown", "ArrowUp", "Home", "End"]);

/**
 * What the palette shows of its search: the field, the groups found, the empty and partial notes and the footer with
 * the scope's button. Tab, and the scope's button, switch the project the palette looks in.
 */
export function PaletteView<T>({
  query,
  onQueryChange,
  pending,
  groups,
  typed,
  partial,
  scope,
  scopeName,
  scopes,
  onSwitchScope,
  onChoose,
}: {
  query: string;
  onQueryChange: (text: string) => void;
  pending: boolean;
  groups: { key: GroupKey; entries: Entry<T>[] }[];
  typed: boolean;
  partial: boolean;
  scope: Scope;
  scopeName: string;
  /** How many scopes Tab cycles through. */
  scopes: number;
  onSwitchScope: (step: 1 | -1) => void;
  onChoose: (target: T) => void;
}) {
  const t = useTranslations("palette");
  const input = useRef<HTMLInputElement>(null);
  // The first item stays selected while results arrive, until the person moves the selection (cmdk alone keeps the
  // item it selected first, which may sit second once a group above it has loaded).
  const [selected, setSelected] = useState("");
  const [moved, setMoved] = useState(false);
  const count = groups.reduce((total, group) => total + group.entries.length, 0);

  const values = groups.flatMap((group) => group.entries.map((entry) => entry.value));
  const value = moved && values.includes(selected) ? selected : (values[0] ?? "");
  const changeQuery = (text: string) => {
    onQueryChange(text);
    setMoved(false);
  };
  const switchScope = (step: 1 | -1) => {
    onSwitchScope(step);
    setMoved(false);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "Tab" || event.altKey || event.ctrlKey || event.metaKey || scopes < 2) return;
    // Tab switches the project instead of leaving the field (the dialog's focus trap would wrap it otherwise).
    event.preventDefault();
    event.stopPropagation();
    switchScope(event.shiftKey ? -1 : 1);
  };
  return (
    <Command
      label={t("label")}
      shouldFilter={false}
      loop
      vimBindings={false}
      value={value}
      onValueChange={setSelected}
      onKeyDown={(event) => {
        if (MOVE_KEYS.has(event.key)) setMoved(true);
      }}
      data-testid="palette-command"
    >
      <CommandInput
        ref={input}
        value={query}
        onValueChange={changeQuery}
        onKeyDown={onKeyDown}
        placeholder={t("placeholder")}
        maxLength={200}
        data-testid="palette-input"
      >
        {pending ? <LoaderCircle className="size-4 shrink-0 animate-spin" aria-hidden="true" data-testid="palette-pending" /> : null}
        <DialogClose asChild>
          <button
            type="button"
            aria-label={t("close")}
            title={t("close")}
            className="inline-flex shrink-0 cursor-pointer items-center justify-center rounded-xs text-fg-subtle transition-colors hover:text-foreground max-md:-mr-2 max-md:size-11 max-md:rounded-sm max-md:hover:bg-accent"
            data-testid="palette-close"
          >
            <Kbd className="max-md:hidden">Esc</Kbd>
            <X className="size-5 md:hidden" aria-hidden="true" />
          </button>
        </DialogClose>
      </CommandInput>
      <CommandList
        label={t("list")}
        className="max-h-[min(380px,calc(100dvh-11rem))]"
        onPointerMove={() => setMoved(true)}
        data-testid="palette-list"
      >
        {groups.map((group) =>
          group.entries.length > 0 ? (
            <CommandGroup key={group.key} heading={t(`groups.${group.key}`)} data-testid={`palette-group-${group.key}`}>
              {group.entries.map((entry) => (
                <CommandItem
                  key={entry.value}
                  value={entry.value}
                  onSelect={() => onChoose(entry.target)}
                  data-testid="palette-item"
                  data-item={entry.value}
                >
                  <entry.icon aria-hidden="true" />
                  <span className="flex min-w-0 flex-1 items-center gap-2">{entry.label}</span>
                  {entry.meta ? (
                    <CommandShortcut className="max-w-[45%] max-sm:max-w-[40%]">
                      <span className="truncate">{entry.meta}</span>
                    </CommandShortcut>
                  ) : null}
                  {entry.shortcut ? <ShortcutKeys id={entry.shortcut} className={entry.meta ? undefined : "ml-auto"} /> : null}
                  <Kbd className="hidden md:group-data-[selected=true]/command-item:inline-flex" aria-hidden="true">
                    ↵
                  </Kbd>
                </CommandItem>
              ))}
            </CommandGroup>
          ) : null,
        )}
      </CommandList>
      {count === 0 && typed && !pending ? (
        <p className="px-4 pt-2 pb-5 text-center text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="palette-empty">
          {t("noMatch", { query: query.trim(), scope: scopeName })}
          {scopes > 1 ? <span className="max-md:hidden"> {t("trySwitch")}</span> : null}
        </p>
      ) : null}
      {partial ? (
        <p className="flex items-center gap-2 border-t px-4 py-2 text-xs text-danger" data-testid="palette-partial">
          <TriangleAlert className="size-3.5 shrink-0" aria-hidden="true" />
          {t("partial")}
        </p>
      ) : null}
      <p className="sr-only" aria-live="polite" data-testid="palette-count">
        {pending ? t("searching") : t("results", { count, scope: scopeName })}
      </p>
      <div className="flex items-center gap-x-4 gap-y-2 border-t px-4 py-2 text-xs text-fg-subtle max-md:py-0">
        <span className="inline-flex items-center gap-1.5 max-md:hidden" aria-hidden="true">
          <Kbd>↑</Kbd>
          <Kbd>↓</Kbd>
          {t("hints.move")}
        </span>
        <span className="inline-flex items-center gap-1.5 max-md:hidden" aria-hidden="true">
          <Kbd>↵</Kbd>
          {t("hints.open")}
        </span>
        {scopes > 1 ? (
          <span className="inline-flex items-center gap-1.5 max-md:hidden" aria-hidden="true">
            <Kbd>Tab</Kbd>
            {t("hints.scope")}
          </span>
        ) : null}
        <button
          type="button"
          onClick={() => {
            switchScope(1);
            input.current?.focus();
          }}
          disabled={scopes < 2}
          aria-label={t("scope.switch", { scope: scopeName })}
          title={t("scope.switch", { scope: scopeName })}
          className="ml-auto inline-flex h-6 min-w-0 cursor-pointer items-center gap-1.5 rounded-xs px-1.5 font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:cursor-default disabled:hover:bg-transparent max-md:-mr-1.5 max-md:h-11"
          data-testid="palette-scope"
          data-scope={scope ?? ""}
        >
          <FolderKanban className="size-3.5 shrink-0" aria-hidden="true" />
          <span className="truncate">{scopeName}</span>
        </button>
      </div>
    </Command>
  );
}
