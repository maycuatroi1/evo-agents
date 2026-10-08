"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleAlert, ExternalLink, Info, Link2, Link2Off, LoaderCircle, Smartphone } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useEffect, useRef, useState } from "react";

import { InlineError, useWriteFailure, type WriteFailure } from "@/components/admin/notice";
import { useNow } from "@/components/kg/use-now";
import { notify } from "@/components/feedback/toast";
import { CopyButton } from "@/components/kg/badges";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";

import {
  makeTelegramLink,
  type TelegramLink,
  type TelegramStatus,
  type TelegramView,
  telegramKeys,
  telegramQuery,
  telegramView,
  unlinkTelegram,
} from "./telegram";

function unauthorized(error: unknown) {
  if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
}

/** A box of the kit inside the dialog: `info` on `surface-sunken`, `warning` on `attention-soft`. */
function Callout({ tone, children, testId }: { tone: "info" | "warning"; children: ReactNode; testId: string }) {
  const Icon = tone === "info" ? Info : CircleAlert;
  return (
    <div
      className={
        tone === "info"
          ? "flex items-start gap-2.5 rounded-md bg-surface-sunken px-3 py-2.5 text-sm text-foreground"
          : "flex items-start gap-2.5 rounded-md border border-attention/30 bg-attention-soft px-3 py-2.5 text-sm text-foreground"
      }
      data-testid={testId}
    >
      <Icon className={tone === "info" ? "mt-0.5 size-4 shrink-0 text-muted-foreground" : "mt-0.5 size-4 shrink-0 text-attention"} aria-hidden="true" />
      <div className="flex min-w-0 flex-col gap-1 text-pretty [overflow-wrap:anywhere]">{children}</div>
    </div>
  );
}

/**
 * The Inbox's Telegram button and its dialog (docs/notifications.md, Telegram). The dialog says where the member's
 * link stands and offers what fits: a one-time link, opened in Telegram, that links the chat its Start is pressed in
 * (the dialog asks the hub every 3 seconds until it is, and says so when it expires after 10 minutes), Unlink, confirmed
 * in place, and a new link once Telegram refused the chat. On a hub without a bot it says only that. A refusal stays in
 * the dialog; a link made and an unlink are said in a toast.
 */
export function TelegramButton() {
  const t = useTranslations("inbox.telegram");
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  return (
    <>
      <Button ref={opener} type="button" variant="outline" onClick={() => setOpen(true)} aria-haspopup="dialog" data-testid="inbox-telegram">
        <Smartphone aria-hidden="true" />
        {t("button")}
      </Button>
      <TelegramDialog open={open} onOpenChange={setOpen} returnFocus={() => opener.current} />
    </>
  );
}

export function TelegramDialog({
  open,
  onOpenChange,
  returnFocus,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** What takes focus once the dialog closes: the button that opened it, even after its content changed under focus. */
  returnFocus?: () => HTMLElement | null;
}) {
  const t = useTranslations("inbox.telegram");
  const format = useFormatter();
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const [link, setLink] = useState<TelegramLink | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<WriteFailure | null>(null);
  // The browser's clock while a link is out; before its first tick a link just made counts as not expired.
  const now = useNow(open && link !== null) ?? 0;
  const pending = link !== null && Date.parse(link.expires_at) > now;
  const status = useQuery({ ...telegramQuery(browserApi, open && pending), enabled: open });
  const view: TelegramView | null = status.data ? telegramView(status.data, link, now) : null;

  // The chat got linked while the link waited: say so once, and forget the link.
  const announced = useRef<string | null>(null);
  useEffect(() => {
    if (view !== "linked" || link === null || announced.current === link.url) return;
    announced.current = link.url;
    notify({ tone: "success", text: t("linkedToast"), description: t("linkedToastText") });
  }, [view, link, t]);

  const make = useMutation({
    mutationFn: () => makeTelegramLink(browserApi()),
    onError: unauthorized,
  });
  const unlink = useMutation({
    mutationFn: () => unlinkTelegram(browserApi()),
    onError: unauthorized,
    onSuccess: (next: TelegramStatus) => queryClient.setQueryData(telegramKeys.status, next),
  });
  const busy = make.isPending || unlink.isPending;
  // The hub's own words for a 502 or 503 (Telegram did not answer, the hub has no bot) say more than "try again".
  const refused = (failed: unknown): WriteFailure => {
    const base = failure(failed);
    const message = isApiError(failed) ? failed.info.message : "";
    return base.status >= 500 && message ? { ...base, detail: message } : base;
  };

  const makeLink = () => {
    if (busy) return;
    setError(null);
    make.mutate(undefined, {
      onSuccess: (made) => {
        setLink(made);
        void queryClient.invalidateQueries({ queryKey: telegramKeys.status });
      },
      onError: (failed) => setError(refused(failed)),
    });
  };
  const doUnlink = () => {
    if (busy) return;
    setError(null);
    unlink.mutate(undefined, {
      onSuccess: () => {
        setConfirming(false);
        setLink(null);
        notify({ tone: "success", text: t("unlinkedToast"), description: t("unlinkedToastText") });
      },
      onError: (failed) => setError(refused(failed)),
    });
  };
  const close = (next: boolean) => {
    if (next || busy) return;
    setConfirming(false);
    setError(null);
    if (view !== "pending") setLink(null);
    onOpenChange(false);
  };

  const data = status.data;
  const linkedAt = data?.linked_at ? format.dateTime(new Date(data.linked_at), { dateStyle: "medium", timeStyle: "short" }) : "";
  const expiresAt = link ? format.dateTime(new Date(link.expires_at), { timeStyle: "short" }) : "";

  let body: ReactNode = null;
  let actions: ReactNode = null;
  if (status.isPending) {
    body = (
      <div className="grid gap-2" aria-busy="true" data-testid="telegram-loading">
        <Skeleton className="h-4 w-3/4" />
        <Skeleton className="h-4 w-1/2" />
      </div>
    );
  } else if (status.isError || !data || view === null) {
    body = <InlineError text={t("loadFailed")} />;
    actions = (
      <Button type="button" size="lg" variant="secondary" onClick={() => void status.refetch()} data-testid="telegram-retry">
        {t("retry")}
      </Button>
    );
  } else if (confirming) {
    body = (
      <Callout tone="warning" testId="telegram-confirm-unlink">
        <p className="font-medium">{t("confirm.title")}</p>
        <p className="text-muted-foreground">{t("confirm.text")}</p>
      </Callout>
    );
    actions = (
      <>
        <Button type="button" size="lg" variant="outline" onClick={() => setConfirming(false)} aria-disabled={busy || undefined} data-testid="telegram-keep">
          {t("confirm.keep")}
        </Button>
        <Button type="button" size="lg" variant="destructive" busy={unlink.isPending} onClick={doUnlink} data-testid="telegram-unlink-confirm">
          <Link2Off aria-hidden="true" />
          {unlink.isPending ? t("confirm.unlinking") : t("confirm.unlink")}
        </Button>
      </>
    );
  } else if (view === "unconfigured") {
    body = (
      <Callout tone="info" testId="telegram-unconfigured">
        <p>{t("unconfigured")}</p>
      </Callout>
    );
  } else if (view === "unlinked" || view === "expired") {
    body =
      view === "expired" ? (
        <Callout tone="warning" testId="telegram-expired">
          <p>{t("expired")}</p>
        </Callout>
      ) : (
        <p className="text-sm text-pretty text-muted-foreground" data-testid="telegram-unlinked">
          {t("unlinked")}
        </p>
      );
    actions = (
      <Button type="button" size="lg" busy={make.isPending} onClick={makeLink} data-testid="telegram-make-link">
        <Link2 aria-hidden="true" />
        {make.isPending ? t("making") : view === "expired" ? t("makeAgain") : t("make")}
      </Button>
    );
  } else if (view === "pending" && link) {
    body = (
      <div className="grid gap-3" data-testid="telegram-pending">
        <ol className="grid list-decimal gap-2 pl-5 text-sm text-pretty marker:text-muted-foreground">
          <li>{t("steps.open", { bot: link.bot })}</li>
          <li>{t("steps.start")}</li>
        </ol>
        <div className="flex min-w-0 items-center gap-1 rounded-sm bg-surface-sunken py-1 pr-1 pl-3">
          <code className="min-w-0 flex-1 truncate font-mono text-xs text-foreground" title={link.url} data-testid="telegram-link">
            {link.url}
          </code>
          <CopyButton value={link.url} label={t("copy")} />
        </div>
        <p className="flex items-center gap-2 text-sm text-muted-foreground" role="status" aria-live="polite" data-testid="telegram-waiting">
          <LoaderCircle className="size-4 shrink-0 animate-spin motion-reduce:animate-none" aria-hidden="true" />
          {t("waiting", { time: expiresAt })}
        </p>
      </div>
    );
    actions = (
      <>
        <Button type="button" size="lg" variant="outline" busy={make.isPending} onClick={makeLink} data-testid="telegram-make-link">
          {make.isPending ? t("making") : t("makeAgain")}
        </Button>
        <Button size="lg" asChild>
          <a href={link.url} target="_blank" rel="noopener noreferrer" data-testid="telegram-open">
            <ExternalLink aria-hidden="true" />
            {t("open")}
            <span className="sr-only">{t("newTab")}</span>
          </a>
        </Button>
      </>
    );
  } else if (view === "linked") {
    body = (
      <div className="grid gap-1.5 text-sm text-pretty" data-testid="telegram-linked">
        <p className="font-medium text-foreground">
          {data.username ? t("linkedAs", { username: data.username }) : t("linkedChat")}
        </p>
        <p className="text-muted-foreground">{t("linkedSince", { when: linkedAt })}</p>
      </div>
    );
    actions = (
      <Button type="button" size="lg" variant="quiet-danger" onClick={() => setConfirming(true)} data-testid="telegram-unlink">
        <Link2Off aria-hidden="true" />
        {t("unlink")}
      </Button>
    );
  } else if (view === "off") {
    body = (
      <Callout tone="warning" testId="telegram-off">
        <p className="font-medium">{t("off")}</p>
        {data.disabled_reason ? <p className="text-muted-foreground">{data.disabled_reason}</p> : null}
      </Callout>
    );
    actions = (
      <>
        <Button type="button" size="lg" variant="quiet-danger" onClick={() => setConfirming(true)} data-testid="telegram-unlink">
          <Link2Off aria-hidden="true" />
          {t("unlink")}
        </Button>
        <Button type="button" size="lg" busy={make.isPending} onClick={makeLink} data-testid="telegram-make-link">
          <Link2 aria-hidden="true" />
          {make.isPending ? t("making") : t("makeAgain")}
        </Button>
      </>
    );
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent
        className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-md"
        onCloseAutoFocus={(event) => {
          const target = returnFocus?.();
          if (!target) return;
          event.preventDefault();
          target.focus();
        }}
        data-testid="telegram-dialog"
        data-view={view ?? "loading"}
      >
        <DialogHeader>
          <DialogTitle className="pr-8">{t("title")}</DialogTitle>
          <DialogDescription className="text-pretty">{t("description")}</DialogDescription>
        </DialogHeader>
        {body}
        {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
        <DialogFooter>
          {confirming ? null : (
            <Button type="button" size="lg" variant="outline" onClick={() => close(false)} aria-disabled={busy || undefined} data-testid="telegram-close">
              {t("close")}
            </Button>
          )}
          {actions}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
