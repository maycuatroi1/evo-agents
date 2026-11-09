"use client";

import { useQuery } from "@tanstack/react-query";
import { CircleCheck, CircleDot, MessageSquarePlus, MessagesSquare, RotateCcw, TextQuote, X } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ReactNode, useEffect, useState } from "react";

import { notify, notifyFailure } from "@/components/feedback/toast";
import { useWriteFailure } from "@/components/feedback/notice";
import { StatusPill } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";

import { reopenComment, srsCommentsQuery } from "./api";
import { CommentForm, ThreadView } from "./comments";
import { signInAgainOn401, useDocumentRefresh } from "./hooks";
import { findQuote, flatten, type PassageThread, type Picked, pickedIn, rangeOf, scopeOf, threadsOn } from "./passage-model";

const HIGHLIGHT = "srs-comment";
const ACTIVE = "srs-comment-active";

type Registry = { set: (name: string, value: unknown) => void; delete: (name: string) => void };

/** The page's registry of CSS highlights, when the browser has one (``::highlight(srs-comment)`` in globals.css). */
function registry(): { highlights: Registry; make: (ranges: Range[]) => unknown } | null {
  const css = typeof CSS === "undefined" ? null : (CSS as unknown as { highlights?: Registry });
  const Make = typeof window === "undefined" ? undefined : (window as unknown as { Highlight?: new (...ranges: Range[]) => unknown }).Highlight;
  return css?.highlights && Make ? { highlights: css.highlights, make: (ranges) => new Make(...ranges) } : null;
}

/** Where each thread's passage is in `container` now (null: not found), by the thread's id. */
function located(container: HTMLElement | null, threads: PassageThread[]): Map<number, Range | null> {
  const found = new Map<number, Range | null>();
  if (!container) return found;
  for (const { root } of threads) {
    if (!root.quote) continue;
    const flat = flatten(scopeOf(container, root.item));
    const at = findQuote(flat, root.quote, root.prefix);
    found.set(root.id, at ? rangeOf(flat, at.start, at.end) : null);
  }
  return found;
}

/**
 * The selection of a reader inside `container` as a passage to comment on, and where to show the button that does
 * it: on a mouse up or a key up that leaves a selection inside it; cleared when the selection goes.
 */
function usePicked(element: HTMLElement | null, mainKey: string, enabled: boolean) {
  const [picked, setPicked] = useState<{ passage: Picked; top: number; left: number } | null>(null);
  useEffect(() => {
    if (!element || !enabled) return;
    const read = () => {
      const selection = window.getSelection();
      if (!selection || selection.isCollapsed || selection.rangeCount === 0) return setPicked(null);
      const range = selection.getRangeAt(0);
      const passage = pickedIn(element, range, mainKey);
      if (!passage) return setPicked(null);
      const box = range.getBoundingClientRect();
      const frame = element.getBoundingClientRect();
      setPicked({ passage, top: box.bottom - frame.top + 6, left: Math.max(0, box.left - frame.left) });
    };
    element.addEventListener("mouseup", read);
    element.addEventListener("keyup", read);
    return () => {
      element.removeEventListener("mouseup", read);
      element.removeEventListener("keyup", read);
    };
  }, [element, mainKey, enabled]);
  return [picked, setPicked] as const;
}

function ReopenButton({ project, slug, id }: { project: string; slug: string; id: number }) {
  const t = useTranslations("srs.passages");
  const refresh = useDocumentRefresh(project, slug);
  const failure = useWriteFailure();
  const [busy, setBusy] = useState(false);
  const reopen = async () => {
    setBusy(true);
    try {
      await reopenComment(browserApi(), project, slug, id);
      await refresh();
      notify({ tone: "success", text: t("reopened") });
    } catch (error) {
      signInAgainOn401(error);
      notifyFailure(t("reopenFailed"), failure(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Button type="button" variant="ghost" size="sm" busy={busy} onClick={() => void reopen()} data-testid="srs-passage-reopen">
      <RotateCcw aria-hidden="true" />
      {t("reopen")}
    </Button>
  );
}

/**
 * Comments on passages of an item: `attach` is the ref of the element that holds the item's text (which needs `relative`
 * positioning), `button` inside it, and `panel` beside it. The panel lists the threads of the item and the items under
 * it: each passage of an open thread highlighted where it is still found, one that is not marked "passage changed", the
 * thread named by `?c=` open and scrolled to. A writer selects text, presses Comment and writes the first comment;
 * anyone in the thread answers it, a writer resolves or reopens it. `@ba` in a comment asks @ba to answer.
 */
export function usePassageComments({
  project,
  slug,
  version,
  itemKey,
  keys,
  writer,
}: {
  project: string;
  slug: string;
  version: number;
  itemKey: string;
  keys: ReadonlySet<string>;
  writer: boolean;
}): { attach: (element: HTMLDivElement | null) => void; button: ReactNode; panel: ReactNode } {
  const t = useTranslations("srs.passages");
  const [container, setContainer] = useState<HTMLDivElement | null>(null);
  const focus = Number(useSearchParams()?.get("c") ?? "") || null;
  const comments = useQuery({ ...srsCommentsQuery(browserApi, project, slug), retry: false });
  const threads = threadsOn(Array.isArray(comments.data) ? comments.data : [], keys);
  const [hovered, setActive] = useState<number | null>(null);
  const active = hovered ?? focus;
  const [picked, setPicked] = usePicked(container, itemKey, writer);
  const [composing, setComposing] = useState<Picked | null>(null);
  const [places, setPlaces] = useState<Map<number, Range | null>>(new Map());
  const signature = threads.map(({ root }) => `${root.id}:${root.resolved_at ?? ""}`).join(",");

  // Find each passage again once the text is drawn, and whenever the threads change.
  useEffect(() => {
    const frame = requestAnimationFrame(() => setPlaces(located(container, threads)));
    return () => cancelAnimationFrame(frame);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- threads is rebuilt each render; its signature says when it changed
  }, [container, signature, version, itemKey]);

  // Highlight the passages of open threads, the active one apart.
  useEffect(() => {
    const css = registry();
    if (!css) return;
    const open = threads.filter(({ root }) => root.resolved_at === null && root.id !== active);
    const ranges = open.map(({ root }) => places.get(root.id)).filter((range): range is Range => Boolean(range));
    css.highlights.set(HIGHLIGHT, css.make(ranges));
    const own = active ? places.get(active) : null;
    if (own) css.highlights.set(ACTIVE, css.make([own]));
    else css.highlights.delete(ACTIVE);
    return () => {
      css.highlights.delete(HIGHLIGHT);
      css.highlights.delete(ACTIVE);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- as above
  }, [places, active, signature]);

  // Scroll to the thread a link names.
  useEffect(() => {
    if (!focus || !comments.data) return;
    document.getElementById(`srs-passage-thread-${focus}`)?.scrollIntoView({ block: "center" });
  }, [focus, comments.data]);

  const start = () => {
    if (!picked) return;
    setComposing(picked.passage);
    setPicked(null);
    window.getSelection()?.removeAllRanges();
  };

  const panel = (
    <Card size="sm" data-testid="srs-passages">
      <CardHeader>
        <CardTitle>
          <h2 className="inline-flex items-center gap-1.5">
            <MessagesSquare className="size-4" aria-hidden="true" />
            {t("title")}
          </h2>
        </CardTitle>
        <p className="text-xs text-fg-subtle">{writer ? t("hint") : t("hintReader")}</p>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {composing ? (
          <div className="flex flex-col gap-2 rounded-md border border-brand/40 p-3" data-testid="srs-passage-composer">
            <div className="flex items-start gap-2">
              <TextQuote className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
              <blockquote className="min-w-0 flex-1 text-[13px] text-muted-foreground italic [overflow-wrap:anywhere]">{composing.quote}</blockquote>
              <Button type="button" variant="ghost" size="icon-sm" aria-label={t("cancel")} onClick={() => setComposing(null)}>
                <X aria-hidden="true" />
              </Button>
            </div>
            <CommentForm
              project={project}
              slug={slug}
              target={{ item: composing.item, version, quote: composing.quote, prefix: composing.prefix, suffix: composing.suffix, field: composing.field, start_offset: composing.offset }}
              label={t("newLabel")}
              submitLabel={t("send")}
              onDone={() => setComposing(null)}
            />
          </div>
        ) : null}
        {threads.length === 0 && !composing ? <p className="text-[13px] text-muted-foreground">{t("none")}</p> : null}
        {threads.map(({ root, replies }) => {
          const open = root.resolved_at === null;
          const lost = root.quote !== null && root.quote !== undefined && places.has(root.id) && places.get(root.id) === null;
          return (
            <div
              key={root.id}
              id={`srs-passage-thread-${root.id}`}
              className={`flex flex-col gap-2 rounded-md ${active === root.id ? "ring-2 ring-brand" : ""}`}
              data-testid="srs-passage-thread"
              data-thread={root.id}
              data-lost={lost || undefined}
              onMouseEnter={() => setActive(root.id)}
              onFocus={() => setActive(root.id)}
            >
              {root.quote ? (
                <div className="flex flex-wrap items-center gap-2 px-1">
                  <blockquote className="min-w-0 flex-1 border-l-2 border-brand/60 pl-2 text-[13px] text-muted-foreground italic [overflow-wrap:anywhere]">{root.quote}</blockquote>
                  {lost ? (
                    <StatusPill look={{ tone: "attention", icon: CircleDot }} label={t("changed")} data-testid="srs-passage-changed" />
                  ) : null}
                </div>
              ) : null}
              <ThreadView project={project} slug={slug} thread={{ root, replies }} writer={writer} />
              {!open && writer ? (
                <div className="flex items-center gap-2 px-1 text-xs text-muted-foreground">
                  <CircleCheck className="size-3.5" aria-hidden="true" />
                  <span className="flex-1">{t("resolvedBy", { login: root.resolved_by ?? "" })}</span>
                  <ReopenButton project={project} slug={slug} id={root.id} />
                </div>
              ) : null}
            </div>
          );
        })}
      </CardContent>
    </Card>
  );

  const button =
    picked && writer ? (
      <Button
        type="button"
        size="sm"
        className="absolute z-10 shadow-raised"
        style={{ top: picked.top, left: picked.left }}
        onMouseDown={(event) => event.preventDefault()}
        onClick={start}
        data-testid="srs-passage-comment"
      >
        <MessageSquarePlus aria-hidden="true" />
        {t("comment")}
      </Button>
    ) : null;
  return { attach: setContainer, button, panel };
}
