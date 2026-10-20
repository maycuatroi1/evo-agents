"use client";

import { RefreshCw, X } from "lucide-react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { DialogClose, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";

import { code, LoginCommand } from "./login-fields";
import type { Account } from "./queries";

/**
 * Renew an account that signed in with OAuth: the page runs no OAuth flow, it tells which command to run in a
 * terminal. The command, run again for the same name, replaces the tokens and keeps the account.
 */
export function OAuthRenewBody({ account }: { account: Account }) {
  const t = useTranslations("accounts.oauthRenew");
  const close = useTranslations("accounts.login")("close");
  return (
    <>
      <DialogHeader className="flex-row items-start gap-3">
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <DialogTitle className="flex items-center gap-2 text-lg font-semibold break-words">
            <RefreshCw className="size-5 shrink-0 text-muted-foreground" aria-hidden="true" />
            <span className="min-w-0 [overflow-wrap:anywhere]">{t("title", { name: account.name })}</span>
          </DialogTitle>
          <DialogDescription className="text-pretty">
            {t.rich(account.state === "needs-relogin" ? "descriptionRelogin" : "description", { code })}
          </DialogDescription>
        </div>
        <DialogClose asChild>
          <Button type="button" variant="ghost" size="icon-lg" className="-mt-1 -mr-1 shrink-0" aria-label={close}>
            <X aria-hidden="true" />
          </Button>
        </DialogClose>
      </DialogHeader>
      <LoginCommand command={`evo-agents hub account oauth-login ${account.name}`} complete />
      <DialogFooter>
        <DialogClose asChild>
          <Button type="button" variant="outline" size="lg" data-testid="oauth-renew-close">
            {close}
          </Button>
        </DialogClose>
      </DialogFooter>
    </>
  );
}
