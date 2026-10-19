"use client";

import { Bot, CircleCheck, CircleDot, MessageSquareText, Send } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { RunRef } from "@/components/data/identifier";
import { SafeMarkdown } from "@/components/data/markdown";
import { When } from "@/components/data/when";
import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { projectHref } from "@/components/shell/nav";
import { StatusPill } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";

import { addComment, resolveComment, type SrsComment, type SrsCommentIn } from "./api";
import { CodeChip, VersionChip } from "./badges";
import { signInAgainOn401, useDocumentRefresh } from "./hooks";
import { itemHref } from "./links";
import type { Thread } from "./model";

export const MAX_COMMENT_CHARS = 20_000; // evo_agents.hub.tables_srs.MAX_COMMENT_CHARS

/** A box that writes a comment: on the document, a version, an item, or as an answer to a thread. */
export function CommentForm({
  project,
  slug,
  target,
  label,
  submitLabel,
  compact = false,
  onDone,
}: {
  project: string;
  slug: string;
  target: Omit<SrsCommentIn, "body">;
  label: string;
  submitLabel: string;
  compact?: boolean;
  onDone?: () => void;
}) {
  const t = useTranslations("srs.comments");
  const id = useId();
  const refresh = useDocumentRefresh(project, slug);
  const failure = useWriteFailure();
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const text = body.trim();
    if (!text || busy) return;
    setBusy(true);
    setRefused(null);
    try {
      await addComment(browserApi(), project, slug, { ...target, body: text });
      setBody("");
      await refresh();
      notify({ tone: "success", text: t("added") });
      onDone?.();
    } catch (error) {
      signInAgainOn401(error);
      setRefused(failure(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-2" data-testid="srs-comment-form">
      <Label htmlFor={id} className={compact ? "sr-only" : undefined}>
        {label}
      </Label>
      <Textarea id={id} value={body} onChange={(event) => setBody(event.target.value)} rows={compact ? 2 : 3} maxLength={MAX_COMMENT_CHARS} />
      {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
      <Button type="submit" variant="secondary" size="sm" busy={busy} disabled={!body.trim()} className="self-end" data-testid="srs-comment-submit">
        <Send aria-hidden="true" />
        {submitLabel}
      </Button>
    </form>
  );
}

function Message({ project, comment }: { project: string; comment: SrsComment }) {
  const t = useTranslations("srs.comments");
  return (
    <div className="flex flex-col gap-1" data-testid="srs-comment" data-comment-id={comment.id}>
      <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
        <span className="font-medium text-foreground">{comment.author}</span>
        {comment.run_id ? (
          <span className="inline-flex items-center gap-1">
            <Bot className="size-3.5" aria-hidden="true" />
            <RunRef id={comment.run_id} href={projectHref(project, `runs/${comment.run_id}`)} label={t("openRun", { id: comment.run_id })} />
          </span>
        ) : null}
        <When value={comment.created_at} />
      </p>
      <SafeMarkdown testId="srs-comment-body">{comment.body}</SafeMarkdown>
    </div>
  );
}

/**
 * A thread: what it is about (an item's code, a version), whether it is open or who resolved it, its messages oldest
 * first, then for a writer an answer box and Resolve while it is open.
 */
export function ThreadView({ project, slug, thread, writer }: { project: string; slug: string; thread: Thread; writer: boolean }) {
  const t = useTranslations("srs.comments");
  const failure = useWriteFailure();
  const refresh = useDocumentRefresh(project, slug);
  const [busy, setBusy] = useState(false);
  const { root, replies } = thread;
  const open = root.resolved_by === null;
  const resolve = async () => {
    setBusy(true);
    try {
      await resolveComment(browserApi(), project, slug, root.id);
      await refresh();
      notify({ tone: "success", text: t("resolved") });
    } catch (error) {
      signInAgainOn401(error);
      notifyFailure(t("resolveFailed"), failure(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <article className="flex flex-col gap-3 rounded-md border bg-card p-4" aria-label={t("threadLabel", { author: root.author })} data-testid="srs-thread" data-open={open}>
      <header className="flex flex-wrap items-center gap-2">
        <MessageSquareText className="size-4 text-muted-foreground" aria-hidden="true" />
        {root.item_code ? <CodeChip code={root.item_code} href={itemHref(project, slug, root.item_code)} /> : <span className="text-[13px]">{root.item ? t("onItem") : t("onDocument")}</span>}
        {root.version ? <VersionChip number={root.version} /> : null}
        <StatusPill
          look={open ? { tone: "neutral", icon: CircleDot } : { tone: "success", icon: CircleCheck }}
          label={open ? t("open") : t("resolvedBy", { login: root.resolved_by ?? "" })}
          data-testid="srs-thread-state"
        />
        {writer && open ? (
          <Button type="button" variant="ghost" size="sm" className="ml-auto" busy={busy} onClick={() => void resolve()} data-testid="srs-thread-resolve">
            {t("resolve")}
          </Button>
        ) : null}
      </header>
      <div className="flex flex-col gap-3 divide-y [&>*]:pt-3 [&>*:first-child]:pt-0">
        {[root, ...replies].map((comment) => (
          <Message key={comment.id} project={project} comment={comment} />
        ))}
      </div>
      {writer && open ? (
        <CommentForm project={project} slug={slug} target={{ parent_id: root.id }} label={t("replyLabel")} submitLabel={t("reply")} compact />
      ) : null}
    </article>
  );
}
