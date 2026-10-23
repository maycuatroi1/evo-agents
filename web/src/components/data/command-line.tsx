"use client";

import { Check, Copy } from "lucide-react";
import { useTranslations } from "next-intl";
import { useRef } from "react";

import { useClipboard } from "@/components/data/identifier";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * A shell command to copy: the text, wrapped at its spaces and anywhere in a long word (a URL, a code) so the whole
 * command shows at any width, and a button that copies it and says so in a toast. Where the clipboard is refused, the
 * button selects the text instead so the person can copy it by hand.
 */
export function CommandLine({ command, label, testId }: { command: string; label: string; testId?: string }) {
  const t = useTranslations("workers.copy");
  const code = useRef<HTMLElement>(null);
  const { state, copy } = useClipboard(command, code, { copied: t("copiedLong"), selected: t("selected") });

  return (
    <div
      role="group"
      aria-label={label}
      className="flex min-w-0 items-stretch overflow-hidden rounded-md border bg-muted/60"
      data-testid={testId}
    >
      {/* Focusable so a keyboard reaches the text the button selects where the clipboard is refused. */}
      <code
        ref={code}
        className="min-w-0 flex-1 self-center px-3 py-2 font-mono text-xs leading-5 whitespace-pre-wrap text-foreground [overflow-wrap:anywhere]"
        tabIndex={0}
        data-command={command}
      >
        {command}
      </code>
      <Button
        type="button"
        variant="ghost"
        onClick={() => void copy()}
        className={cn("h-auto shrink-0 cursor-pointer rounded-none border-l bg-card px-3 text-xs hover:bg-accent")}
        aria-label={t("label", { what: label })}
      >
        {state === "copied" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
        <span aria-hidden="true">{state === "copied" ? t("copied") : t("copy")}</span>
      </Button>
    </div>
  );
}
