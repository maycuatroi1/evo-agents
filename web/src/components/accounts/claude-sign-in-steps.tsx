"use client";

import { ExternalLink, RotateCw } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode, RefObject } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

import { FieldError } from "./login-fields";
import { ProviderLogo } from "./provider-logo";
import type { useSignInFlow } from "./sign-in-queries";

function Step({ index, done, title, children }: { index: number; done?: boolean; title: ReactNode; children: ReactNode }) {
  return (
    <li className="grid grid-cols-[1.75rem_minmax(0,1fr)] gap-3">
      <span
        className={cn(
          "flex size-7 items-center justify-center rounded-full border text-xs font-semibold tabular-nums",
          done ? "border-brand/40 bg-surface-selected text-foreground" : "bg-card text-muted-foreground",
        )}
        aria-hidden="true"
      >
        {index}
      </span>
      <div className="flex min-w-0 flex-col gap-2 pt-0.5">
        <h3 className="text-sm font-medium">{title}</h3>
        {children}
      </div>
    </li>
  );
}

/**
 * The two steps of Claude's sign-in from the page: open claude.ai (a link, so no popup blocker stands in the way, in a
 * new tab without an opener) and approve, then paste the code it shows. The link waits for the hub's flow; a flow that
 * failed to start says so with Retry.
 */
export function ClaudeSignInSteps({
  sign,
  codeRef,
  codeId,
  codeProblem,
  onCodeChange,
}: {
  sign: ReturnType<typeof useSignInFlow>;
  codeRef: RefObject<HTMLInputElement | null>;
  codeId: string;
  codeProblem: string | null;
  onCodeChange: () => void;
}) {
  const t = useTranslations("accounts.login.web");
  const started = sign.flow.data;
  return (
    <ol className="flex flex-col gap-5" data-testid="login-web-steps">
      <Step index={1} done={sign.opened} title={t("openTitle")}>
        <p className="text-[13px] leading-5 text-pretty text-muted-foreground">{t("openText")}</p>
        {sign.flow.isError ? (
          <div className="flex flex-wrap items-center gap-2" role="alert">
            <span className="text-sm text-danger">{t("startFailed")}</span>
            <Button type="button" variant="outline" size="sm" onClick={() => void sign.flow.refetch()} data-testid="login-web-retry">
              <RotateCw aria-hidden="true" />
              {t("retry")}
            </Button>
          </div>
        ) : started ? (
          <Button asChild variant={sign.opened ? "outline" : "default"} size="lg" className="w-full sm:w-auto sm:self-start">
            <a href={started.authorize_url} target="_blank" rel="noopener noreferrer" onClick={sign.open} data-testid="login-web-open">
              <ProviderLogo provider="claude" className={cn("size-4", !sign.opened && "rounded-xs bg-white p-px")} />
              {sign.opened ? t("reopen") : t("open")}
              <ExternalLink aria-hidden="true" className="opacity-70" />
              <span className="sr-only">{t("newTab")}</span>
            </a>
          </Button>
        ) : (
          <Skeleton className="h-10 w-full sm:w-52" aria-label={t("preparing")} data-testid="login-web-preparing" />
        )}
      </Step>
      <Step index={2} title={<Label htmlFor={codeId}>{t("codeTitle")}</Label>}>
        <Input
          ref={codeRef}
          id={codeId}
          defaultValue=""
          onChange={onCodeChange}
          placeholder={t("codePlaceholder")}
          autoComplete="off"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          className="h-10 font-mono placeholder:font-sans"
          aria-invalid={codeProblem ? true : undefined}
          aria-describedby={`${codeId}-hint${codeProblem ? ` ${codeId}-error` : ""}`}
          aria-required="true"
          data-1p-ignore="true"
          data-lpignore="true"
          data-bwignore="true"
          data-form-type="other"
          data-testid="login-web-code"
        />
        <p id={`${codeId}-hint`} className="text-[13px] leading-5 text-pretty text-muted-foreground">
          {t("codeHint")}
        </p>
        <FieldError id={`${codeId}-error`} text={codeProblem} testId="login-web-code-error" />
      </Step>
    </ol>
  );
}
