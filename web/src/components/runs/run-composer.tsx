"use client";

import { useMutation } from "@tanstack/react-query";
import { CircleCheck, Send } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError, type WriteFailure } from "@/components/admin/notice";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { cn } from "@/lib/utils";

import { useRunFailure } from "./hooks";
import { MAX_MESSAGE_BYTES, type Run, sendRunMessage } from "./queries";
import { utf8Bytes } from "./run-model";

/** The byte count shows once a message passes this share of the limit. */
const COUNT_FROM = 0.75;

/**
 * The owner's box for messages to the run's agent, under the log. The hub keeps a message in the run's inbox and
 * adds it to the log; the worker hands it to the agent at its next turn. Ctrl or Cmd + Enter sends. A blank message
 * or one over 8 KiB of UTF-8 is refused here with the reason, next to the box; a refusal of the hub shows there too.
 */
export function RunComposer({ run }: { run: Pick<Run, "project" | "id"> }) {
  const t = useTranslations("runs.detail.composer");
  const format = useFormatter();
  const ids = useId();
  const failure = useRunFailure();
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

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-2 border-t px-4 py-3" aria-busy={send.isPending || undefined} data-testid="run-composer">
      <Label htmlFor={`${ids}-text`}>{t("label")}</Label>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
        <Textarea
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
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) submit();
          }}
          className="max-h-48 min-h-16 resize-y"
          aria-invalid={problem ? true : undefined}
          aria-describedby={describedBy}
          data-testid="run-composer-text"
        />
        <Button type="submit" size="lg" className="shrink-0" busy={send.isPending} data-testid="run-composer-send">
          <Send aria-hidden="true" />
          {send.isPending ? t("sending") : t("send")}
        </Button>
      </div>
      {problem ? (
        <p id={`${ids}-problem`} className="text-xs font-medium text-danger" data-testid="run-composer-problem">
          {problem === "blank" ? t("blank") : t("long", { max: format.number(MAX_MESSAGE_BYTES) })}
        </p>
      ) : null}
      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <p id={`${ids}-hint`} className="text-pretty">
          {t("hint")}
        </p>
        <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
          {/* Always in the page, so a sent message is announced. */}
          <span aria-live="polite" className="font-medium text-success" data-testid="run-composer-sent">
            {sent ? (
              <span className="inline-flex items-center gap-1.5">
                <CircleCheck className="size-3.5 shrink-0" aria-hidden="true" />
                {t("sent")}
              </span>
            ) : null}
          </span>
          {bytes >= MAX_MESSAGE_BYTES * COUNT_FROM ? (
            <span className={cn("tabular-nums", long && "font-medium text-danger")}>
              {t("bytes", { count: format.number(bytes), max: format.number(MAX_MESSAGE_BYTES) })}
            </span>
          ) : null}
        </span>
      </div>
    </form>
  );
}
