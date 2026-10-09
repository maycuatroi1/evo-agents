"use client";

import { Check, Copy } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type ComponentProps, type ReactNode, type RefObject, useEffect, useRef, useState } from "react";

import { notify } from "@/components/feedback/toast";
import { cn } from "@/lib/utils";

/**
 * Names, not states (web/DESIGN.md, Shape): a worker, a branch, a revision, a session, a hash. A name sits in a mono
 * chip on `surface-sunken` with square-ish 4 px corners; a run is `#N` in mono. Links among them read as text and turn
 * brand on hover, like the name in the first column of a table (`NAME_LINK`).
 */

/** A name that links to its page, in a table's first column or a list's primary cell: text colour, brand on hover. */
export const NAME_LINK = "font-medium text-foreground transition-colors hover:text-brand";

const CHIP =
  "inline-flex h-5 max-w-full min-w-0 items-center rounded-xs bg-surface-sunken px-1.5 align-middle font-mono text-xs leading-4 whitespace-nowrap";

export type CopyState = "idle" | "copied" | "selected";

/**
 * Copies `value` to the clipboard, or selects the text of `target` where the clipboard is refused (an insecure origin,
 * a denied permission), so Ctrl+C or Cmd+C still works; a toast says which ("<value> copied to the clipboard" unless
 * `words` says it otherwise). The state, for the button's own icon, goes back to idle after 2 seconds.
 */
export function useClipboard(
  value: string,
  target: RefObject<HTMLElement | null> | null,
  words: { copied?: string; selected?: string } = {},
) {
  const t = useTranslations("identifier");
  const [state, setState] = useState<CopyState>("idle");
  useEffect(() => {
    if (state === "idle") return;
    const timer = setTimeout(() => setState("idle"), 2_000);
    return () => clearTimeout(timer);
  }, [state]);

  /** Selects the text of `target`; false when there is none to select. */
  const select = (): boolean => {
    const node = target?.current;
    const selection = typeof window === "undefined" ? null : window.getSelection();
    if (!node || !selection) return false;
    const range = document.createRange();
    range.selectNodeContents(node);
    selection.removeAllRanges();
    selection.addRange(range);
    return true;
  };
  const copy = async () => {
    try {
      if (!navigator.clipboard) throw new Error("no clipboard");
      await navigator.clipboard.writeText(value);
      setState("copied");
      notify({ tone: "success", text: words.copied ?? t("copied", { value }) });
    } catch {
      if (select()) {
        setState("selected");
        notify({ tone: "info", text: words.selected ?? t("selected") });
      } else {
        notify({ tone: "info", text: t("refused") });
      }
    }
  };
  return { state, copy };
}

/** Copies `value`, or selects the chip's text where the clipboard is refused, and says which in a toast. */
function CopyButton({ value, label, target }: { value: string; label: string; target: RefObject<HTMLElement | null> }) {
  const { state, copy } = useClipboard(value, target);
  return (
    <>
      <button
        type="button"
        onClick={() => void copy()}
        aria-label={label}
        title={label}
        data-testid="identifier-copy"
        className="relative inline-grid size-6 shrink-0 place-items-center rounded-xs text-fg-subtle transition-colors after:absolute after:-inset-2.5 hover:bg-accent hover:text-foreground md:after:hidden [&_svg]:size-3.5"
      >
        {state === "copied" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
      </button>
    </>
  );
}

/**
 * A name in a mono chip: a worker, branch, revision, session or hash. `href` makes it a link (text colour, brand on
 * hover); `externalHref` makes it a link out of the hub instead (a branch or a commit on its forge), opened in a new
 * tab without an opener or a referrer and named by `linkLabel` ("7c1e9a2 (opens in a new tab)"); only http and https
 * addresses are taken. `copy` adds a button that copies `value`, named by `copyLabel` ("Copy branch name") or
 * "Copy <value>". `children` is what shows when it differs from `value`, such as a short hash; `title` then carries
 * the whole value.
 */
export function Identifier({
  value,
  children,
  href,
  externalHref,
  linkLabel,
  copy = false,
  copyLabel,
  title,
  className,
  testId,
  ...data
}: {
  value: string;
  children?: ReactNode;
  href?: Route;
  externalHref?: string | null;
  linkLabel?: string;
  copy?: boolean;
  copyLabel?: string;
  title?: string;
  className?: string;
  testId?: string;
} & { [key: `data-${string}`]: string | number | undefined }) {
  const t = useTranslations("identifier");
  const text = useRef<HTMLElement>(null);
  const shown = children ?? value;
  const external = externalHref && /^https?:\/\//i.test(externalHref) ? externalHref : null;
  const chip = external ? (
    <a
      ref={text as RefObject<HTMLAnchorElement | null>}
      href={external}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={linkLabel}
      title={title}
      className={cn(CHIP, "text-foreground transition-colors hover:text-brand", className)}
      data-testid={testId}
      {...data}
    >
      <span className="truncate">{shown}</span>
    </a>
  ) : href ? (
    <Link
      ref={text as RefObject<HTMLAnchorElement | null>}
      href={href}
      title={title}
      className={cn(CHIP, "text-foreground transition-colors hover:text-brand", className)}
      data-testid={testId}
      {...data}
    >
      <span className="truncate">{shown}</span>
    </Link>
  ) : (
    <span
      ref={text}
      title={title}
      className={cn(CHIP, "text-muted-foreground", className)}
      data-testid={testId}
      {...data}
    >
      <span className="truncate">{shown}</span>
    </span>
  );
  if (!copy) return chip;
  return (
    <span className="inline-flex max-w-full min-w-0 items-center gap-0.5 align-middle">
      {chip}
      <CopyButton value={value} label={copyLabel ?? t("copy", { value })} target={text} />
    </span>
  );
}

/**
 * A run as `#N` in mono with tabular figures. With `href` it links to the run in text colour, brand on hover; `label`
 * then names the link for screen readers ("Open run #7").
 */
export function RunRef({
  id,
  href,
  label,
  className,
  ...data
}: {
  id: number;
  href?: Route;
  label?: string;
  className?: string;
} & { [key: `data-${string}`]: string | number | undefined }) {
  const style = "font-mono text-[13px] whitespace-nowrap tabular-nums";
  if (!href) {
    return (
      <span className={cn(style, "text-muted-foreground", className)} {...data}>
        #{id}
      </span>
    );
  }
  return (
    <Link
      href={href}
      aria-label={label}
      className={cn(style, "font-medium text-foreground transition-colors hover:text-brand", className)}
      {...data}
    >
      #{id}
    </Link>
  );
}

/**
 * A kind or a role as a tag: an icon and a word on `surface-sunken` with 4 px corners. Square-ish because it names
 * what something is (Admin, Plan run), never a state that changes.
 */
export function Tag({
  children,
  className,
  ...props
}: { children: ReactNode; className?: string } & Omit<ComponentProps<"span">, "children" | "className">) {
  return (
    <span
      data-slot="tag"
      className={cn(
        "inline-flex h-[18px] w-fit max-w-full shrink-0 items-center gap-1 rounded-xs bg-surface-sunken px-1.5 text-[11px] leading-none font-medium whitespace-nowrap text-muted-foreground [&>svg]:size-3 [&>svg]:shrink-0",
        className,
      )}
      {...props}
    >
      {children}
    </span>
  );
}
