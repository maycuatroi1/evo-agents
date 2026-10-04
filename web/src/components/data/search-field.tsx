"use client";

import { Search, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

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
  className?: string;
  testId?: string;
};

/**
 * A search box inside a search landmark: a visible-to-assistive-technology label, the query committed after a pause
 * in typing or on Enter, Escape or the clear button to empty it. Debouncing keeps one request per pause, not one
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
  className,
  testId,
}: SearchFieldProps) {
  const id = useId();
  const [draft, setDraft] = useState(value);
  const [committed, setCommitted] = useState(value);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

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
      role="search"
      aria-label={label}
      className={cn("relative w-full", className)}
      onSubmit={(event) => {
        event.preventDefault();
        commit(draft);
      }}
    >
      <label htmlFor={id} className="sr-only">
        {label}
      </label>
      <Search
        className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground"
        aria-hidden="true"
      />
      <Input
        id={id}
        type="search"
        value={draft}
        maxLength={maxLength}
        placeholder={placeholder}
        autoComplete="off"
        spellCheck={false}
        enterKeyHint="search"
        className="h-9 pr-9 pl-8 [&::-webkit-search-cancel-button]:hidden"
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
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          className="absolute top-1/2 right-1 -translate-y-1/2"
          aria-label={clearLabel}
          onClick={() => {
            setDraft("");
            commit("");
          }}
        >
          <X aria-hidden="true" />
        </Button>
      ) : null}
    </form>
  );
}
