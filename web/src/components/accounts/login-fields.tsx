"use client";

import { Check, Copy, type LucideIcon } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useRef } from "react";

import { useClipboard } from "@/components/data/identifier";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

/** The parts of the Add Claude account dialog: hints and problems, notes, the checklists, the steps and the command. */

export function Hint({ id, children }: { id: string; children: ReactNode }) {
  return (
    <p id={id} className="text-xs text-pretty text-muted-foreground">
      {children}
    </p>
  );
}

export function FieldError({ id, text, testId }: { id: string; text: string | null; testId: string }) {
  if (!text) return null;
  return (
    <p id={id} className="text-xs font-medium text-danger" data-testid={testId}>
      {text}
    </p>
  );
}

/** Inline code in a sentence: a command or a variable. */
export const code = (chunks: ReactNode) => <code className="rounded-xs bg-muted px-1 font-mono text-xs text-foreground">{chunks}</code>;

/** A note in the form: neutral on `muted`, or amber when the person should act on it, with what to do as `children`. */
export function Note({
  tone,
  icon: Icon,
  text,
  children,
  live,
  testId,
}: {
  tone: "info" | "attention";
  icon: LucideIcon;
  text: ReactNode;
  children?: ReactNode;
  /** Said by a screen reader when it appears, for a note that follows what the person types. */
  live?: boolean;
  testId?: string;
}) {
  return (
    <div
      role={live ? "status" : undefined}
      className={cn(
        "flex items-start gap-2.5 rounded-md border px-3 py-2.5 text-sm",
        tone === "attention" ? "border-attention/20 bg-attention-soft text-attention" : "bg-muted text-foreground",
      )}
      data-testid={testId}
    >
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <div className="flex min-w-0 flex-col items-start gap-2">
        <p className="text-pretty">{text}</p>
        {children}
      </div>
    </div>
  );
}

/**
 * Names to pick with checkboxes, two to a row from `sm`: the projects or the workers of an account. A picked one is
 * `surface-selected` inside a brand edge, and its box checked, so colour is not the only cue.
 */
export function CheckList({
  idPrefix,
  names,
  picked,
  onToggle,
  invalid,
  testId,
}: {
  idPrefix: string;
  names: readonly string[];
  picked: readonly string[];
  onToggle: (name: string, on: boolean) => void;
  invalid?: boolean;
  testId: string;
}) {
  return (
    <div className="grid gap-2 sm:grid-cols-2" data-testid={testId}>
      {names.map((name, index) => (
        <label
          key={name}
          htmlFor={`${idPrefix}-${index}`}
          className={cn(
            "flex min-h-10 cursor-pointer items-center gap-2.5 rounded-md border px-3 py-2 transition-colors hover:bg-accent max-md:min-h-11",
            picked.includes(name) && "border-brand/40 bg-surface-selected hover:bg-surface-selected",
          )}
        >
          <input
            id={`${idPrefix}-${index}`}
            type="checkbox"
            value={name}
            checked={picked.includes(name)}
            onChange={(event) => onToggle(name, event.target.checked)}
            className="size-4 shrink-0 cursor-pointer accent-primary"
            aria-invalid={invalid || undefined}
          />
          <span className="min-w-0 font-mono text-sm [overflow-wrap:anywhere]">{name}</span>
        </label>
      ))}
    </div>
  );
}

/** Two checkbox rows while their names load. */
export function CheckListSkeleton() {
  return (
    <div className="grid gap-2 sm:grid-cols-2" aria-hidden="true">
      <Skeleton className="h-10 w-full" />
      <Skeleton className="h-10 w-full" />
    </div>
  );
}

/** A dashed box that says why there is nothing to pick. */
export function EmptyPick({ children, testId }: { children: ReactNode; testId: string }) {
  return (
    <p className="rounded-md border border-dashed px-3 py-2.5 text-sm text-pretty text-muted-foreground" data-testid={testId}>
      {children}
    </p>
  );
}

/** What a renewal keeps: the account's projects and workers, read only. */
export function KeptBindings({ projects, workers }: { projects: readonly string[]; workers: readonly string[] }) {
  const t = useTranslations("accounts.login");
  const value = "min-w-0 font-mono text-[13px] [overflow-wrap:anywhere]";
  return (
    <dl className="grid gap-x-4 gap-y-2 rounded-md border px-3 py-2.5 text-sm sm:grid-cols-[auto_minmax(0,1fr)]" data-testid="login-kept">
      <dt className="text-muted-foreground">{t("projects")}</dt>
      <dd className={value} data-testid="login-kept-projects">
        {projects.join(", ") || "-"}
      </dd>
      <dt className="text-muted-foreground">{t("workers")}</dt>
      <dd className={workers.length ? value : "text-fg-subtle"} data-testid="login-kept-workers">
        {workers.length ? workers.join(", ") : t("anyWorker")}
      </dd>
    </dl>
  );
}

/** The numbered steps to get a token from `claude setup-token`, ending at the field it is pasted in. */
export function SetupSteps() {
  const t = useTranslations("accounts.login.steps");
  const steps = [t.rich("run", { code }), t("approve"), t("paste")];
  return (
    <ol className="flex flex-col gap-2.5" data-testid="login-steps">
      {steps.map((step, index) => (
        <li key={index} className="grid grid-cols-[1.5rem_minmax(0,1fr)] items-start gap-2.5 text-sm">
          <span
            className="flex size-6 items-center justify-center rounded-full bg-accent text-xs font-semibold text-accent-foreground tabular-nums"
            aria-hidden="true"
          >
            {index + 1}
          </span>
          <span className="pt-0.5 text-pretty">{step}</span>
        </li>
      ))}
    </ol>
  );
}

/**
 * The `hub account login` command to copy: the text in a code line that scrolls when it is wider than the dialog,
 * and a button that copies it and says so in a toast, or selects it where the clipboard is refused. While a field the
 * command needs is missing, the button is off and the line shows a placeholder in its place.
 */
export function LoginCommand({ command, complete }: { command: string; complete: boolean }) {
  const t = useTranslations("accounts.login.command");
  const line = useRef<HTMLElement>(null);
  const { state, copy } = useClipboard(command, line, { copied: t("copied"), selected: t("selected") });
  return (
    <div
      role="group"
      aria-label={t("label")}
      className="flex min-w-0 items-stretch overflow-hidden rounded-md border bg-muted/60"
      data-testid="login-command"
    >
      {/* Focusable so a keyboard can scroll a command wider than the dialog. */}
      <code
        ref={line}
        className="min-w-0 flex-1 overflow-x-auto px-3 py-2 font-mono text-xs leading-5 whitespace-nowrap text-foreground"
        tabIndex={0}
        data-testid="login-command-text"
      >
        {command}
      </code>
      <Button
        type="button"
        variant="ghost"
        onClick={() => void copy()}
        disabled={!complete}
        className="h-auto shrink-0 cursor-pointer rounded-none border-l bg-card px-3 text-xs hover:bg-accent"
        aria-label={t("copyLabel")}
        data-testid="login-command-copy"
      >
        {state === "copied" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
        <span aria-hidden="true">{state === "copied" ? t("copiedShort") : t("copy")}</span>
      </Button>
    </div>
  );
}
