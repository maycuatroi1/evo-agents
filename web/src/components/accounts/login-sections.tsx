"use client";

import { ChevronRight, RefreshCw } from "lucide-react";
import { useTranslations } from "next-intl";
import { type RefObject, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

import { BindingPick, CheckList, CheckListSkeleton, code, EmptyPick, FieldError, Hint, LoginCommand, Note, SetupSteps } from "./login-fields";
import { type LoginField, type LoginForm, loginCommand, type LoginProblems, utcDay } from "./login-model";
import { MAX_SECRET_NAME_CHARS } from "./login-queries";
import { ProviderLogo } from "./provider-logo";
import type { Account } from "./queries";

/** How the token reaches the hub: Claude's sign-in from this page (the default), or a token pasted from a terminal. */
export type Method = "web" | "token";

/** The two ways to connect, as a segmented control (a radio group): Sign in with Claude, or Paste a token. */
export function MethodSwitch({ method, onChange }: { method: Method; onChange: (method: Method) => void }) {
  const t = useTranslations("accounts.login.method");
  return (
    <div role="radiogroup" aria-label={t("label")} className="grid grid-cols-2 gap-1 rounded-lg bg-muted p-1" data-testid="login-method">
      {(["web", "token"] as const).map((item) => (
        <button
          key={item}
          type="button"
          role="radio"
          aria-checked={method === item}
          onClick={() => onChange(item)}
          className={cn(
            "flex min-h-9 cursor-pointer items-center justify-center gap-2 rounded-md px-3 text-sm font-medium text-muted-foreground transition-colors max-md:min-h-11",
            "focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
            method === item ? "bg-card text-foreground shadow-xs" : "hover:text-foreground",
          )}
          data-testid={`login-method-${item}`}
        >
          {item === "web" ? <ProviderLogo provider="claude" className="size-4" /> : null}
          {t(item)}
        </button>
      ))}
    </div>
  );
}

/**
 * The workers or pools a new account is kept to, folded under one line that says what is picked ("Any of yours" when
 * nothing is): most accounts go to any worker of their owner, so the lists stay out of the way until asked for.
 */
export function BindingsDisclosure({
  idPrefix,
  form,
  loading,
  choices,
  onToggle,
}: {
  idPrefix: string;
  form: LoginForm;
  loading: { workers: boolean; pools: boolean };
  choices: { workers: readonly string[]; pools: readonly string[] };
  onToggle: (list: "workers" | "pools", name: string, on: boolean) => void;
}) {
  const t = useTranslations("accounts.login");
  const bound = form.workers.length + form.pools.length;
  return (
    <details className="group rounded-md border" open={bound > 0 || undefined} data-testid="login-bindings">
          <summary className="flex min-h-10 cursor-pointer list-none items-center gap-2 px-3 py-2 text-sm max-md:min-h-11 [&::-webkit-details-marker]:hidden">
            <ChevronRight className="size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" aria-hidden="true" />
            <span className="font-medium">{t("bindingsTitle")}</span>
            <span className="ml-auto truncate text-muted-foreground">
              {form.pools.length
                ? t("bindingsPools", { count: form.pools.length })
                : form.workers.length
                  ? t("bindingsWorkers", { count: form.workers.length })
                  : t("anyWorker")}
            </span>
          </summary>
          <div className="flex flex-col gap-4 border-t px-3 py-3">
            {(["workers", "pools"] as const).map((list) => (
              <BindingPick
                key={list}
                id={`${idPrefix}-${list}`}
                list={list}
                loading={loading[list]}
                names={choices[list]}
                picked={form[list]}
                locked={(list === "workers" ? form.pools : form.workers).length > 0}
                onToggle={(name, on) => onToggle(list, name, on)}
              />
            ))}
          </div>
        </details>
  );
}

/**
 * The token of `claude setup-token`, pasted: the steps, the password field (no name attribute, so no submission the
 * browser makes by itself carries it), the day it ends, and the `hub account login` command that does the same from a
 * terminal with nothing to paste.
 */
export function TokenMethod({
  ids,
  tokenRef,
  form,
  renewing,
  now,
  errors,
  problem,
  describedBy,
  onExpires,
  onTokenChange,
}: {
  ids: string;
  tokenRef: RefObject<HTMLInputElement | null>;
  form: LoginForm;
  renewing: boolean;
  now: number;
  errors: LoginProblems;
  problem: (field: LoginField) => string | null;
  describedBy: (field: LoginField) => string;
  onExpires: (day: string) => void;
  onTokenChange: () => void;
}) {
  const t = useTranslations("accounts.login");
  const command = loginCommand(form, { renewing, now });
  const [minDay] = useState(() => utcDay(now + 24 * 60 * 60 * 1000));
  return (
        <div className="flex flex-col gap-5" data-testid="login-token-method">
          <div className="flex flex-col gap-3 rounded-md border px-3 py-3">
            <SetupSteps />
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${ids}-token`}>{t("token")}</Label>
              {/* No name attribute: the token is never part of a form submission the browser could make by itself. */}
              <Input
                ref={tokenRef}
                id={`${ids}-token`}
                type="password"
                defaultValue=""
                onChange={onTokenChange}
                autoComplete="off"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                className="h-9 font-mono"
                aria-invalid={errors.token ? true : undefined}
                aria-describedby={describedBy("token")}
                aria-required="true"
                data-1p-ignore="true"
                data-lpignore="true"
                data-bwignore="true"
                data-form-type="other"
                data-testid="login-token"
              />
              <Hint id={`${ids}-token-hint`}>{t("tokenHint")}</Hint>
              <FieldError id={`${ids}-token-error`} text={problem("token")} testId="login-token-error" />
            </div>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${ids}-expires`}>{t("expires")}</Label>
            <Input
              id={`${ids}-expires`}
              name="expires_at"
              type="date"
              min={minDay}
              value={form.expires}
              onChange={(event) => onExpires(event.target.value)}
              className="h-9 w-full tabular-nums sm:w-48"
              aria-invalid={errors.expires ? true : undefined}
              aria-describedby={describedBy("expires")}
              aria-required="true"
              data-testid="login-expires"
            />
            <Hint id={`${ids}-expires-hint`}>{t("expiresHint")}</Hint>
            <FieldError id={`${ids}-expires-error`} text={problem("expires")} testId="login-expires-error" />
          </div>
          <div className="flex flex-col gap-2">
            <h3 className="text-sm font-medium">{t("commandTitle")}</h3>
            <p className="text-[13px] leading-[18px] text-pretty text-muted-foreground">{t.rich(renewing ? "commandRenew" : "commandAdd", { code })}</p>
            <LoginCommand command={command.text} complete={command.complete} />
            {command.complete ? null : <Hint id={`${ids}-command-hint`}>{t("commandIncomplete")}</Hint>}
          </div>
        </div>
  );
}

/** The projects whose runs may take a new account: those where the visitor is a writer, as checkboxes. */
export function ProjectsPick({
  ids,
  state,
  names,
  picked,
  invalid,
  problem,
  describedBy,
  onToggle,
}: {
  ids: string;
  state: "loading" | "failed" | "none" | "ready";
  names: readonly string[];
  picked: readonly string[];
  invalid: boolean;
  problem: string | null;
  describedBy: string;
  onToggle: (name: string, on: boolean) => void;
}) {
  const t = useTranslations("accounts.login");
  return (
    <fieldset className="flex min-w-0 flex-col gap-2" aria-describedby={describedBy}>
      <legend className="mb-1.5 text-sm font-medium">{t("projects")}</legend>
      {state === "loading" ? (
        <CheckListSkeleton />
      ) : state === "failed" ? (
        <p className="text-sm text-danger" role="alert">
          {t("projectsFailed")}
        </p>
      ) : state === "none" ? (
        <EmptyPick testId="login-no-projects">{t("noProjects")}</EmptyPick>
      ) : (
        <CheckList idPrefix={`${ids}-project`} names={names} picked={picked} onToggle={onToggle} invalid={invalid} testId="login-projects" />
      )}
      <Hint id={`${ids}-projects-hint`}>{t("projectsHint")}</Hint>
      <FieldError id={`${ids}-projects-error`} text={problem} testId="login-projects-error" />
    </fieldset>
  );
}

/**
 * A new account's name, suggested and editable. A name that is one of the visitor's Claude accounts already says so,
 * with a button that turns the Add into that account's renewal.
 */
export function NameField({
  ids,
  value,
  invalid,
  problem,
  describedBy,
  taken,
  onChange,
  onRenewInstead,
}: {
  ids: string;
  value: string;
  invalid: boolean;
  problem: string | null;
  describedBy: string;
  taken: Account | null;
  onChange: (name: string) => void;
  onRenewInstead: (account: Account) => void;
}) {
  const t = useTranslations("accounts.login");
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={`${ids}-name`}>{t("name")}</Label>
      <Input
        id={`${ids}-name`}
        name="name"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={t("namePlaceholder")}
        autoComplete="off"
        autoCapitalize="none"
        spellCheck={false}
        maxLength={MAX_SECRET_NAME_CHARS}
        className="h-9 font-mono placeholder:font-sans"
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
        aria-required="true"
        data-testid="login-name"
      />
      <Hint id={`${ids}-name-hint`}>{t("nameHint")}</Hint>
      <FieldError id={`${ids}-name-error`} text={problem} testId="login-name-error" />
      {taken ? (
        <Note tone="attention" icon={RefreshCw} text={t("nameTaken", { name: taken.name })} live testId="login-name-taken">
          <Button type="button" variant="outline" size="sm" onClick={() => onRenewInstead(taken)} data-testid="login-renew-instead">
            {t("renewInstead", { name: taken.name })}
          </Button>
        </Note>
      ) : null}
    </div>
  );
}
