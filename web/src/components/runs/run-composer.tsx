"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowUp, CircleCheck } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError, type WriteFailure } from "@/components/admin/notice";
import { AttachButton, AttachmentTray, FileDropFrame, pastedFiles, useFileDrop } from "@/components/data/attachments";
import { shortcutText } from "@/components/shell/shortcuts";
import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { FilesNotReady, useAttachments } from "@/hooks/use-attachments";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { runUploadBase } from "@/lib/attachments";
import { LOGIN_PATH } from "@/lib/config";
import { isSendShortcut, useModifierKey } from "@/lib/keyboard";
import { cn } from "@/lib/utils";

import { useRunFailure } from "./hooks";
import { MAX_MESSAGE_BYTES, type Run, runKeys, type RunChat, sendRunMessage } from "./queries";
import { utf8Bytes } from "./run-model";

/** The byte count shows once a message passes this share of the limit. */
const COUNT_FROM = 0.75;

/**
 * The kit's Composer: the owner's box for messages to the run's agent, under the trace. One bordered box (the focus
 * ring on the box, not the field), the files attached over the textarea, the textarea without its own edge, and a bar
 * with Attach, what the run is ("Headless run on laptop") and Send with its key (Cmd or Ctrl with Enter). Files are
 * picked, dropped on the box or pasted into it (data/attachments.tsx), each uploaded at once; Send waits for them. The
 * hub keeps a message in the run's inbox and adds it to the log; the worker hands it to the agent at its next turn. A
 * blank message without files, or one over 8 KiB of UTF-8, is refused here with the reason, next to the box; a refusal
 * of the hub shows there too, and the files stay attached.
 */
export function RunComposer({ run }: { run: Pick<Run, "project" | "id"> & Partial<Pick<Run, "mode" | "worker" | "state">> }) {
  const t = useTranslations("runs.detail.composer");
  const format = useFormatter();
  const interactive = run.state === "interactive" || run.mode === "interactive";
  const where = run.worker ? t(interactive ? "interactiveOn" : "headlessOn", { worker: run.worker }) : t(interactive ? "interactive" : "headless");
  return (
    <FileDropFrame className="rounded-md" data-testid="run-composer-drop">
      <MessageForm
        project={run.project}
        runId={run.id}
        rows={2}
        className="pt-2"
        testIds={{ form: "run-composer", text: "run-composer-text", send: "run-composer-send", sent: "run-composer-sent", problem: "run-composer-problem", hint: "run-composer-hint" }}
        texts={{
          label: t("label"),
          placeholder: t("placeholder"),
          hint: where,
          send: t("send"),
          sending: t("sending"),
          sent: t("sent"),
          blank: t("blank"),
          long: t("long", { max: format.number(MAX_MESSAGE_BYTES) }),
          bytes: (count) => t("bytes", { count: format.number(count), max: format.number(MAX_MESSAGE_BYTES) }),
          conflict: t("conflict"),
        }}
      />
    </FileDropFrame>
  );
}

/**
 * The owner's reply in an author run's Chat tab: the same Composer, posting to the run that takes the chat's next
 * message (a parked run's reply queues the run that resumes it). Files dropped anywhere on the chat come here.
 */
export function ChatReply({ project, chat }: { project: string; chat: RunChat }) {
  const t = useTranslations("runs.author.chat.reply");
  const format = useFormatter();
  return (
    <MessageForm
      project={project}
      runId={chat.run_id}
      rows={3}
      className="border-t px-4 pt-3 pb-4"
      refresh
      testIds={{ form: "run-chat-reply", text: "run-chat-reply-text", send: "run-chat-send", sent: "run-chat-reply-sent", problem: "run-chat-reply-problem" }}
      texts={{
        label: t("label"),
        placeholder: chat.status === "waiting" ? t("placeholderWaiting") : t("placeholderWorking"),
        hint: chat.status === "waiting" ? (chat.state === "parked" ? t("hintParked") : t("hintWaiting")) : t("hintWorking"),
        send: t("send"),
        sending: t("sending"),
        sent: t("sent"),
        blank: t("blank"),
        long: t("long", { max: format.number(MAX_MESSAGE_BYTES) }),
        bytes: (count) => t("bytes", { count: format.number(count), max: format.number(MAX_MESSAGE_BYTES) }),
        conflict: t("conflict"),
      }}
    />
  );
}

type Texts = Record<"label" | "placeholder" | "hint" | "send" | "sending" | "sent" | "blank" | "long" | "conflict", string> & { bytes: (count: number) => string };
type TestIds = { form: string; text: string; send: string; sent: string; problem: string; hint?: string };

function MessageForm({
  project,
  runId,
  rows,
  className,
  refresh = false,
  testIds,
  texts,
}: {
  project: string;
  runId: number;
  rows: number;
  className: string;
  /** Read the project's runs again once a message went (an author run's chat moves on). */
  refresh?: boolean;
  testIds: TestIds;
  texts: Texts;
}) {
  const tFiles = useTranslations("attachments");
  const ids = useId();
  const failure = useRunFailure();
  const modifier = useModifierKey();
  const queryClient = useQueryClient();
  const files = useAttachments(runUploadBase(project, runId));
  useFileDrop(files.add);
  const [text, setText] = useState("");
  const [problem, setProblem] = useState<"blank" | "long" | "files" | null>(null);
  const [error, setError] = useState<WriteFailure | null>(null);
  const [sent, setSent] = useState(false);
  const send = useMutation({
    mutationFn: async (message: string) => sendRunMessage(browserApi(), project, runId, message, await files.settled()),
    onError: (cause) => {
      if (cause instanceof FilesNotReady) return setProblem("files");
      if (isApiError(cause) && cause.kind === "unauthorized") window.location.assign(LOGIN_PATH);
      setError(failure(cause, texts.conflict));
    },
    onSuccess: () => {
      setText("");
      files.clear();
      setSent(true);
    },
    onSettled: () => (refresh ? queryClient.invalidateQueries({ queryKey: runKeys.all(project) }) : undefined),
  });
  const bytes = utf8Bytes(text);
  const long = bytes > MAX_MESSAGE_BYTES;

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (send.isPending) return;
    setSent(false);
    setError(null);
    if (!text.trim() && files.count === 0) return setProblem("blank");
    if (long) return setProblem("long");
    setProblem(null);
    send.mutate(text);
  };

  const describedBy = [`${ids}-hint`, problem ? `${ids}-problem` : null].filter(Boolean).join(" ");
  return (
    <form onSubmit={submit} noValidate className={cn("flex flex-col gap-2", className)} aria-busy={send.isPending || undefined} data-testid={testIds.form}>
      <div
        className={cn(
          "rounded-md border border-input bg-card transition-[border-color,box-shadow] focus-within:border-ring focus-within:ring-1 focus-within:ring-ring",
          problem && "border-danger-solid",
        )}
      >
        <AttachmentTray files={files} testId={testIds.form} />
        <label htmlFor={`${ids}-text`} className="sr-only">
          {texts.label}
        </label>
        <textarea
          id={`${ids}-text`}
          value={text}
          rows={rows}
          placeholder={texts.placeholder}
          onChange={(event) => {
            setText(event.target.value);
            setProblem(null);
            setSent(false);
          }}
          onPaste={(event) => {
            const pasted = pastedFiles(event);
            if (!pasted.length) return;
            event.preventDefault();
            setSent(false);
            files.add(pasted);
          }}
          onKeyDown={(event) => {
            if (isSendShortcut(event.nativeEvent)) {
              event.preventDefault();
              submit();
            }
          }}
          className={cn(
            "block w-full resize-y border-0 bg-transparent p-3 text-sm leading-5 text-foreground outline-none placeholder:text-fg-subtle focus-visible:outline-none max-sm:text-base",
            rows > 2 ? "max-h-60 min-h-[76px]" : "max-h-48 min-h-[60px]",
          )}
          aria-invalid={problem ? true : undefined}
          aria-describedby={describedBy}
          aria-keyshortcuts="Meta+Enter Control+Enter"
          data-testid={testIds.text}
        />
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t p-2">
          <AttachButton onFiles={files.add} testId={testIds.form} />
          <p id={`${ids}-hint`} className="min-w-0 flex-1 basis-40 text-xs text-pretty text-fg-subtle" data-testid={testIds.hint}>
            {texts.hint}
          </p>
          {/* Always in the page, so a sent message is announced. */}
          <span aria-live="polite" className="text-xs font-medium text-success empty:hidden" data-testid={testIds.sent}>
            {sent ? (
              <span className="inline-flex items-center gap-1.5">
                <CircleCheck className="size-3.5 shrink-0" aria-hidden="true" />
                {texts.sent}
              </span>
            ) : null}
          </span>
          {bytes >= MAX_MESSAGE_BYTES * COUNT_FROM ? (
            <span className={cn("text-xs text-muted-foreground tabular-nums", long && "font-medium text-danger")}>{texts.bytes(bytes)}</span>
          ) : null}
          <Button type="submit" size="sm" className="ml-auto shrink-0" busy={send.isPending} aria-keyshortcuts="Meta+Enter Control+Enter" data-testid={testIds.send}>
            <ArrowUp aria-hidden="true" />
            {send.isPending ? texts.sending : texts.send}
            {modifier && !send.isPending ? (
              <Kbd className="ml-0.5 border-current bg-transparent text-current opacity-70" aria-hidden="true">
                {shortcutText("send", modifier)}
              </Kbd>
            ) : null}
          </Button>
        </div>
      </div>
      {problem ? (
        <p id={`${ids}-problem`} className="text-xs font-medium text-danger" data-testid={testIds.problem}>
          {problem === "blank" ? texts.blank : problem === "long" ? texts.long : tFiles("notReady")}
        </p>
      ) : null}
      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
    </form>
  );
}
