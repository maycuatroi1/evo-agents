"use client";

import { useMutation } from "@tanstack/react-query";
import { ArrowUp, CircleCheck } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError, type WriteFailure } from "@/components/admin/notice";
import { shortcutText } from "@/components/shell/shortcuts";
import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { isSendShortcut, useModifierKey } from "@/lib/keyboard";
import { cn } from "@/lib/utils";

import { useRunFailure } from "./hooks";
import { MAX_MESSAGE_BYTES, type Run, sendRunMessage } from "./queries";
import { utf8Bytes } from "./run-model";

/** The byte count shows once a message passes this share of the limit. */
const COUNT_FROM = 0.75;

/**
 * The kit's Composer: the owner's box for messages to the run's agent, under the trace. One bordered box (the focus
 * ring on the box, not the field), the textarea without its own edge, and a bar with what the run is ("Headless run on
 * laptop") and Send with its key (Cmd or Ctrl with Enter). The hub keeps a message in the run's inbox and adds it to
 * the log; the worker hands it to the agent at its next turn. A blank message or one over 8 KiB of UTF-8 is refused
 * here with the reason, next to the box; a refusal of the hub shows there too.
 */
export function RunComposer({ run }: { run: Pick<Run, "project" | "id"> & Partial<Pick<Run, "mode" | "worker" | "state">> }) {
  const t = useTranslations("runs.detail.composer");
  const format = useFormatter();
  const ids = useId();
  const failure = useRunFailure();
  const modifier = useModifierKey();
  const [text, setText] = useState("");
  const [problem, setProblem] = useState<"blank" | "long" | null>(null);
  const [error, setError] = useState<WriteFailure | null>(null);
  const [sent, setSent] = useState(false);
  const send = useMutation({
    mutationFn: (message: string) => sendRunMessage(browserApi(), run.project, run.id, message),
    onError: (cause) => {
      if (isApiError(cause) && cause.kind === "unauthorized") window.location.assign(LOGIN_PATH);
      setError(failure(cause, t("conflict")));
    },
    onSuccess: () => {
      setText("");
      setSent(true);
    },
  });
  const bytes = utf8Bytes(text);
  const long = bytes > MAX_MESSAGE_BYTES;

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (send.isPending) return;
    setSent(false);
    setError(null);
    if (!text.trim()) return setProblem("blank");
    if (long) return setProblem("long");
    setProblem(null);
    send.mutate(text);
  };

  const describedBy = [`${ids}-hint`, problem ? `${ids}-problem` : null].filter(Boolean).join(" ");
  const interactive = run.state === "interactive" || run.mode === "interactive";
  const where = run.worker ? t(interactive ? "interactiveOn" : "headlessOn", { worker: run.worker }) : t(interactive ? "interactive" : "headless");

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-2 px-4 pt-1 pb-4" aria-busy={send.isPending || undefined} data-testid="run-composer">
      <div
        className={cn(
          "rounded-md border border-input bg-card transition-[border-color,box-shadow] focus-within:border-ring focus-within:ring-1 focus-within:ring-ring",
          problem && "border-danger-solid",
        )}
      >
        <label htmlFor={`${ids}-text`} className="sr-only">
          {t("label")}
        </label>
        <textarea
          id={`${ids}-text`}
          value={text}
          rows={2}
          placeholder={t("placeholder")}
          onChange={(event) => {
            setText(event.target.value);
            setProblem(null);
            setSent(false);
          }}
          onKeyDown={(event) => {
            if (isSendShortcut(event.nativeEvent)) {
              event.preventDefault();
              submit();
            }
          }}
          className="block max-h-48 min-h-[60px] w-full resize-y border-0 bg-transparent p-3 text-sm leading-5 text-foreground outline-none placeholder:text-fg-subtle focus-visible:outline-none max-sm:text-base"
          aria-invalid={problem ? true : undefined}
          aria-describedby={describedBy}
          aria-keyshortcuts="Meta+Enter Control+Enter"
          data-testid="run-composer-text"
        />
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t p-2">
          <p id={`${ids}-hint`} className="min-w-0 flex-1 basis-48 pl-1 text-xs text-pretty text-fg-subtle" data-testid="run-composer-hint">
            {where}
          </p>
          {/* Always in the page, so a sent message is announced. */}
          <span aria-live="polite" className="text-xs font-medium text-success empty:hidden" data-testid="run-composer-sent">
            {sent ? (
              <span className="inline-flex items-center gap-1.5">
                <CircleCheck className="size-3.5 shrink-0" aria-hidden="true" />
                {t("sent")}
              </span>
            ) : null}
          </span>
          {bytes >= MAX_MESSAGE_BYTES * COUNT_FROM ? (
            <span className={cn("text-xs text-muted-foreground tabular-nums", long && "font-medium text-danger")}>
              {t("bytes", { count: format.number(bytes), max: format.number(MAX_MESSAGE_BYTES) })}
            </span>
          ) : null}
          <Button type="submit" size="sm" className="ml-auto shrink-0" busy={send.isPending} aria-keyshortcuts="Meta+Enter Control+Enter" data-testid="run-composer-send">
            <ArrowUp aria-hidden="true" />
            {send.isPending ? t("sending") : t("send")}
            {modifier && !send.isPending ? (
              <Kbd className="ml-0.5 border-current bg-transparent text-current opacity-70" aria-hidden="true">
                {shortcutText("send", modifier)}
              </Kbd>
            ) : null}
          </Button>
        </div>
      </div>
      {problem ? (
        <p id={`${ids}-problem`} className="text-xs font-medium text-danger" data-testid="run-composer-problem">
          {problem === "blank" ? t("blank") : t("long", { max: format.number(MAX_MESSAGE_BYTES) })}
        </p>
      ) : null}
      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
    </form>
  );
}
