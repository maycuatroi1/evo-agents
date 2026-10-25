"use client";

import { KeyRound, TriangleAlert } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { CommandLine } from "@/components/data/command-line";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

import { FormError } from "./form-error";
import { useWorkerFailure, useWorkerWrite } from "./hooks";
import { Note } from "./note";
import { bootstrapCommand, MAX_USES, TOKEN_HOURS, userDataCommand } from "./pool-model";
import { createJoinToken, type IssuedJoinToken, type JoinTokenRequest } from "./queries";

function hubUrl(): string {
  return typeof window === "undefined" ? "" : window.location.origin;
}

function whole(text: string, min: number, max: number): number | null {
  const value = Number(text.trim());
  return text.trim() !== "" && Number.isInteger(value) && value >= min && value <= max ? value : null;
}

/**
 * A join token of the pool: how long it works and how many machines may join with it. Once the hub answers, the
 * dialog shows the token and the command that makes a fresh Ubuntu machine a node, this one time: the hub keeps only
 * the token's hash. The content mounts each time the dialog opens, so a token shown before is not shown again.
 */
export function JoinTokenDialog({ pool, open, onOpenChange }: { pool: string; open: boolean; onOpenChange: (open: boolean) => void }) {
  const write = useWorkerWrite((api, body: JoinTokenRequest) => createJoinToken(api, pool, body));
  const pending = write.isPending;
  const guard = (event: Event) => {
    if (pending || write.isSuccess) event.preventDefault();
  };
  return (
    <Dialog open={open} onOpenChange={(next) => (pending ? undefined : onOpenChange(next))}>
      <DialogContent
        className="flex max-h-[calc(100dvh-2rem)] flex-col gap-5 overflow-y-auto sm:max-w-2xl"
        onEscapeKeyDown={(event) => pending && event.preventDefault()}
        onInteractOutside={guard}
        data-testid="join-token-dialog"
      >
        <JoinTokenFlow pool={pool} write={write} />
      </DialogContent>
    </Dialog>
  );
}

type Write = ReturnType<typeof useWorkerWrite<JoinTokenRequest, IssuedJoinToken>>;

function JoinTokenFlow({ pool, write }: { pool: string; write: Write }) {
  const t = useTranslations("workers.pools.join");
  const ids = useId();
  const failure = useWorkerFailure();
  const [hours, setHours] = useState("24");
  const [uses, setUses] = useState("1");
  const [errors, setErrors] = useState<{ hours?: boolean; uses?: boolean }>({});
  const [issued, setIssued] = useState<IssuedJoinToken | null>(null);
  const pending = write.isPending;

  if (issued) return <Issued pool={pool} token={issued} />;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    const body = { hours: whole(hours, TOKEN_HOURS.min, TOKEN_HOURS.max), max_uses: whole(uses, MAX_USES.min, MAX_USES.max) };
    const found = { hours: body.hours === null, uses: body.max_uses === null };
    setErrors(found);
    if (found.hours || found.uses) {
      document.getElementById(found.hours ? `${ids}-hours` : `${ids}-uses`)?.focus();
      return;
    }
    try {
      setIssued(await write.mutateAsync({ hours: body.hours as number, max_uses: body.max_uses as number }));
    } catch {
      // said above the buttons, from the mutation's state
    }
  };
  const error = write.isError ? failure(write.error) : null;

  return (
    <form onSubmit={(event) => void submit(event)} noValidate aria-busy={pending || undefined} className="flex flex-col gap-5">
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2 text-lg font-semibold">
          <KeyRound className="size-5 text-muted-foreground" aria-hidden="true" />
          {t("title", { pool })}
        </DialogTitle>
        <DialogDescription>{t("description")}</DialogDescription>
      </DialogHeader>
      <fieldset disabled={pending} className="grid min-w-0 gap-5 sm:grid-cols-2">
        <div className="flex min-w-0 flex-col gap-1.5">
          <Label htmlFor={`${ids}-hours`}>{t("hours")}</Label>
          <Input
            id={`${ids}-hours`}
            type="number"
            inputMode="numeric"
            min={TOKEN_HOURS.min}
            max={TOKEN_HOURS.max}
            step={1}
            value={hours}
            onChange={(event) => setHours(event.target.value)}
            className="h-9 tabular-nums"
            aria-invalid={errors.hours || undefined}
            aria-describedby={`${ids}-hours-hint`}
            data-testid="join-token-hours"
          />
          <p id={`${ids}-hours-hint`} className={errors.hours ? "text-xs font-medium text-danger" : "text-xs text-muted-foreground"}>
            {t("hoursHint", { min: TOKEN_HOURS.min, max: TOKEN_HOURS.max })}
          </p>
        </div>
        <div className="flex min-w-0 flex-col gap-1.5">
          <Label htmlFor={`${ids}-uses`}>{t("uses")}</Label>
          <Input
            id={`${ids}-uses`}
            type="number"
            inputMode="numeric"
            min={MAX_USES.min}
            max={MAX_USES.max}
            step={1}
            value={uses}
            onChange={(event) => setUses(event.target.value)}
            className="h-9 tabular-nums"
            aria-invalid={errors.uses || undefined}
            aria-describedby={`${ids}-uses-hint`}
            data-testid="join-token-uses"
          />
          <p id={`${ids}-uses-hint`} className={errors.uses ? "text-xs font-medium text-danger" : "text-xs text-muted-foreground"}>
            {t("usesHint", { min: MAX_USES.min, max: MAX_USES.max })}
          </p>
        </div>
      </fieldset>
      {error ? <FormError text={error.text} detail={error.detail} requestId={error.requestId} testId="join-token-error" /> : null}
      <DialogFooter>
        <DialogClose asChild>
          <Button
            type="button"
            variant="outline"
            size="lg"
            aria-disabled={pending || undefined}
            className="aria-disabled:opacity-50"
            onClick={(event) => pending && event.preventDefault()}
          >
            {t("cancel")}
          </Button>
        </DialogClose>
        <Button type="submit" size="lg" busy={pending} data-testid="join-token-create">
          <KeyRound aria-hidden="true" />
          {pending ? t("creating") : t("create")}
        </Button>
      </DialogFooter>
    </form>
  );
}

function Issued({ pool, token }: { pool: string; token: IssuedJoinToken }) {
  const t = useTranslations("workers.pools.join");
  const format = useFormatter();
  const expires = format.dateTime(new Date(token.expires_at), { dateStyle: "medium", timeStyle: "short" });
  return (
    <div className="flex flex-col gap-5" data-testid="join-token-issued">
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2 text-lg font-semibold">
          <KeyRound className="size-5 text-muted-foreground" aria-hidden="true" />
          {t("issuedTitle", { pool })}
        </DialogTitle>
        <DialogDescription>{t("issuedDescription", { uses: token.max_uses, expires })}</DialogDescription>
      </DialogHeader>
      <Note tone="warning" icon={TriangleAlert} testId="join-token-once">
        <p className="font-medium">{t("once")}</p>
        <p>{t("onceText")}</p>
      </Note>
      <div className="flex flex-col gap-2">
        <h3 className="text-sm font-medium">{t("bootstrap")}</h3>
        <p className="text-xs text-muted-foreground">{t("bootstrapHint")}</p>
        <CommandLine command={bootstrapCommand(hubUrl(), token.token)} label={t("bootstrap")} testId="join-token-bootstrap" />
      </div>
      <div className="flex flex-col gap-2">
        <h3 className="text-sm font-medium">{t("token")}</h3>
        <CommandLine command={token.token} label={t("token")} testId="join-token-value" />
      </div>
      <div className="flex flex-col gap-2">
        <h3 className="text-sm font-medium">{t("userData")}</h3>
        <p className="text-xs text-muted-foreground">{t("userDataHint")}</p>
        <CommandLine command={userDataCommand(pool)} label={t("userData")} testId="join-token-user-data" />
      </div>
      <DialogFooter>
        <DialogClose asChild>
          <Button type="button" size="lg" data-testid="join-token-done">
            {t("done")}
          </Button>
        </DialogClose>
      </DialogFooter>
    </div>
  );
}
