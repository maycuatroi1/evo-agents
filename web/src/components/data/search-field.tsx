"use client";

import { Search, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import { Kbd } from "@/components/ui/kbd";
import { useCharacterKeys } from "@/lib/keyboard";
import { cn } from "@/lib/utils";

import { useSearchShortcut } from "./search-shortcut";

type SearchFieldProps = {
  /** The committed query (from the URL); the field follows it when it changes elsewhere. */
  value: string;
  onCommit: (value: string) => void;
  label: string;
  placeholder: string;
  clearLabel: string;
  /** Milliseconds after the last keystroke before the query is committed; Enter commits at once. */
  debounce?: number;
  maxLength?: number;
  /** The `/` key focuses this field (the page's search); off for a second search field on the same page. */
  shortcut?: boolean;
  /** The ids of what the query filters, for `aria-controls`. */
  controls?: string;
  className?: string;
  testId?: string;
};

/**
 * The kit's search input, for a list's toolbar: a magnifier, the field and, while it is empty, the `/` key that
 * focuses it from anywhere on the page; once it holds text, a button that clears it. 32 px tall, 44 px with 16 px
 * text under 768 px so phones do not zoom. The label is for assistive technology; the query is committed after a
 * pause in typing or on Enter, Escape or the clear button empty it. Debouncing keeps one request per pause, not one
 * per keystroke.
 */
export function SearchField({
  value,
  onCommit,
  label,
  placeholder,
  clearLabel,
  debounce = 300,
  maxLength = 500,
  shortcut = true,
  controls,
  className,
  testId,
}: SearchFieldProps) {
  const id = useId();
  const input = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState(value);
  const [committed, setCommitted] = useState(value);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useSearchShortcut(input, shortcut);
  // The key is shown and announced only while single-key shortcuts are on (the shortcuts dialog turns them off).
  const characterKeys = useCharacterKeys();
  const slash = shortcut && characterKeys;

  // The URL changed without this field (back, a link, "clear filters"): show what it says now.
  if (value !== committed) {
    setCommitted(value);
    setDraft(value);
  }

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  const commit = (next: string) => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    const trimmed = next.trim();
    setCommitted(trimmed);
    if (trimmed !== value) onCommit(trimmed);
  };

  const change = (next: string) => {
    setDraft(next);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => commit(next), debounce);
  };

  return (
    <form
      className={cn(
        "flex h-8 w-full min-w-0 items-center gap-2 rounded-sm border border-input bg-card pr-1.5 pl-2.5 text-fg-subtle transition-colors max-md:h-11",
        "focus-within:border-ring focus-within:ring-1 focus-within:ring-ring",
        className,
      )}
      onSubmit={(event) => {
        event.preventDefault();
        commit(draft);
      }}
    >
      <label htmlFor={id} className="sr-only">
        {label}
      </label>
      <Search className="size-4 shrink-0" aria-hidden="true" />
      <input
        ref={input}
        id={id}
        type="search"
        value={draft}
        maxLength={maxLength}
        placeholder={placeholder}
        autoComplete="off"
        spellCheck={false}
        enterKeyHint="search"
        aria-keyshortcuts={slash ? "/" : undefined}
        aria-controls={controls}
        className="h-full min-w-0 flex-1 bg-transparent text-base text-foreground outline-none placeholder:text-fg-subtle focus-visible:outline-none md:text-[13px] [&::-webkit-search-cancel-button]:hidden"
        onChange={(event) => change(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape" && draft) {
            event.preventDefault();
            setDraft("");
            commit("");
          }
        }}
        data-testid={testId}
      />
      {draft ? (
        <button
          type="button"
          aria-label={clearLabel}
          title={clearLabel}
          onClick={() => {
            setDraft("");
            commit("");
            input.current?.focus();
          }}
          className="relative inline-grid size-6 shrink-0 cursor-pointer place-items-center rounded-xs text-fg-subtle transition-colors after:absolute after:-inset-2.5 hover:bg-accent hover:text-foreground md:after:hidden"
        >
          <X className="size-3.5" aria-hidden="true" />
        </button>
      ) : slash ? (
        <Kbd aria-hidden="true" className="mr-0.5 max-md:hidden">
          /
        </Kbd>
      ) : null}
    </form>
  );
}
