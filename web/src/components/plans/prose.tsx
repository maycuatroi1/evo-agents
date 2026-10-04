"use client";

import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { isObject, text } from "@/lib/plans";
import { cn } from "@/lib/utils";

/** Mono text that is data (commands, paths, YAML): JetBrains Mono's ligatures would draw "->" or "|-" as glyphs. */
export const VERBATIM_MONO = "font-mono [font-variant-ligatures:none]";

/**
 * Plan text as people wrote it. YAML folds long text into one line and keeps the line breaks of literal blocks,
 * so prose keeps its line breaks (`pre-line`) at a readable measure; evidence and verify commands are shown
 * verbatim, every space kept, in the mono face (it has the Vietnamese subset).
 */
export function Prose({ children, className }: { children: string; className?: string }) {
  return (
    <p
      className={cn(
        "max-w-prose text-sm leading-relaxed whitespace-pre-line text-pretty [overflow-wrap:anywhere]",
        className,
      )}
    >
      {children.trimEnd()}
    </p>
  );
}

export function Verbatim({ children, testId, className }: { children: string; testId?: string; className?: string }) {
  return (
    <pre
      data-testid={testId}
      className={cn(
        "max-w-full rounded-lg border bg-muted/40 px-3 py-2.5 font-mono text-[13px] leading-relaxed whitespace-pre-wrap [font-variant-ligatures:none] [overflow-wrap:anywhere]",
        className,
      )}
    >
      {children}
    </pre>
  );
}

/** Values with line breaks or long enough to read as a paragraph are prose; short ones stay inline. */
function Scalar({ value }: { value: string }) {
  if (value.includes("\n") || value.length > 80) return <Prose>{value}</Prose>;
  return <span className="text-sm [overflow-wrap:anywhere]">{value}</span>;
}

const MAX_DEPTH = 4;

/** A mapping's keys in `order` first, then the rest alphabetically: the order `evo_agents.hub.mirror` writes. The API
 * keeps plans as jsonb, which does not keep the order of keys. */
export function orderedEntries(value: Record<string, unknown>, order: readonly string[]): [string, unknown][] {
  const rank = (key: string) => {
    const found = order.indexOf(key);
    return found === -1 ? order.length : found;
  };
  return Object.entries(value).sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b));
}

/**
 * Any value of a plan section (acceptance, risks, decisions, ...), shown as it is: text as prose, lists as lists,
 * mappings as term and description. The page names the section; this shows what it holds.
 */
export function ValueView({
  value,
  depth = 0,
  order = [],
}: {
  value: unknown;
  depth?: number;
  /** The order of the keys of a mapping (and of the mappings in a list), as the plan's copy writes them. */
  order?: readonly string[];
}): ReactNode {
  const t = useTranslations("plans");
  const scalar = text(value);
  if (scalar !== null) return <Scalar value={scalar} />;
  if (value === null || value === undefined) return <span className="text-sm text-muted-foreground">-</span>;
  if (depth >= MAX_DEPTH) return <Verbatim>{JSON.stringify(value, null, 2)}</Verbatim>;
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-sm text-muted-foreground">{t("emptyList")}</span>;
    const simple = value.every((item) => text(item) !== null);
    if (simple) {
      return (
        <ul className="flex max-w-prose list-disc flex-col gap-1.5 pl-5 text-sm leading-relaxed marker:text-muted-foreground">
          {value.map((item, index) => (
            <li key={index} className="whitespace-pre-line [overflow-wrap:anywhere]">
              {text(item)?.trimEnd()}
            </li>
          ))}
        </ul>
      );
    }
    return (
      <ol className="flex flex-col gap-2">
        {value.map((item, index) => (
          <li key={index} className="rounded-lg border bg-background/60 px-3 py-2.5">
            <ValueView value={item} depth={depth + 1} order={order} />
          </li>
        ))}
      </ol>
    );
  }
  if (isObject(value)) {
    return (
      <dl className="grid grid-cols-1 gap-x-4 gap-y-1.5 sm:grid-cols-[minmax(6rem,auto)_1fr]">
        {orderedEntries(value, order).map(([key, item]) => (
          <div key={key} className="contents">
            <dt className="pt-0.5 font-mono text-xs text-muted-foreground [font-variant-ligatures:none]">{key}</dt>
            <dd className="min-w-0">
              <ValueView value={item} depth={depth + 1} />
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  return <Verbatim>{String(value)}</Verbatim>;
}
