"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

import { cn } from "@/lib/utils";

import { LinkedText } from "./links";
import { cutOutput } from "./trace-model";

/**
 * The note a hook of the hub leaves on a line of its own in an output it cut, `[evo-hub: ...]` (hub/spool.py), with the
 * line break that ends it. It says where the whole output is, so it wraps at the block's width where the lines around
 * it keep their columns and scroll.
 */
const HUB_NOTE = /^(\[evo-hub: [^\n]*\]$\n?)/m;

/** Plain output: the hub's notes wrap, the rest keeps its lines as written. */
function Output({ text }: { text: string }) {
  const parts = text.split(HUB_NOTE);
  if (parts.length === 1) return <LinkedText text={text} tone="term" />;
  return parts.map((part, index) =>
    index % 2 === 1 ? (
      <span key={index} className="block whitespace-pre-wrap [overflow-wrap:anywhere]" data-testid="trace-hub-note">
        <LinkedText text={part} tone="term" />
      </span>
    ) : part ? (
      <LinkedText key={index} text={part} tone="term" />
    ) : null,
  );
}

/**
 * Text on the terminal surface of the Trace, cut at 20 lines unless the person asks for all of it: a tool's output and
 * change, a system line's output, a step report's verify. `diff` colours `+` and `-` lines; `wrap` breaks long lines
 * (a command) instead of scrolling them.
 */
export function TermBlock({
  text,
  diff = false,
  wrap = false,
  label,
  testId,
}: {
  text: string;
  diff?: boolean;
  wrap?: boolean;
  label?: string;
  testId: string;
}) {
  const t = useTranslations("runs.detail.trace");
  const [whole, setWhole] = useState(false);
  const { shown, total, cut } = cutOutput(text);
  const body = whole ? text.replace(/\n$/, "") : shown;
  return (
    <div className="min-w-0 border-t border-term-border first:border-t-0" data-testid={testId}>
      {label ? <p className="bg-term-bg px-3 pt-2 font-mono text-[11px] leading-4 text-term-muted">{label}</p> : null}
      <pre
        className={cn(
          "m-0 max-h-[32rem] overflow-auto bg-term-bg px-3 py-2.5 font-mono text-xs leading-[18px] text-term-fg [font-variant-ligatures:none]",
          wrap ? "whitespace-pre-wrap [overflow-wrap:anywhere]" : "whitespace-pre",
        )}
        tabIndex={0}
      >
        {diff
          ? body.split("\n").map((line, index) => (
              <span key={index} className={cn("block", line.startsWith("+") ? "text-term-ok" : line.startsWith("-") ? "text-term-error" : "")}>
                {line ? <LinkedText text={line} tone="term" /> : " "}
              </span>
            ))
          : wrap
            ? body.split("\n").map((line, index) => {
                // The line's indent is padding, not spaces a long word would wrap after; what wraps goes on 4ch further
                // in, so it does not read as the next command.
                const indent = /^ */.exec(line)?.[0].length ?? 0;
                return (
                  <span key={index} className="block -indent-[4ch]" style={{ paddingLeft: `${indent + 4}ch` }}>
                    {line.trim() ? <LinkedText text={line.slice(indent)} tone="term" /> : " "}
                  </span>
                );
              })
            : <Output text={body} />}
      </pre>
      {cut ? (
        <button
          type="button"
          className="block w-full border-t border-term-border bg-term-bg px-3 py-1.5 text-left font-mono text-xs text-term-muted hover:bg-term-row hover:text-term-fg max-md:min-h-11"
          aria-expanded={whole}
          onClick={() => setWhole((value) => !value)}
          data-testid="trace-output-more"
        >
          {whole ? t("showLess", { count: 20 }) : t("showFull", { count: total })}
        </button>
      ) : null}
    </div>
  );
}
