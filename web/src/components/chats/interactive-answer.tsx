"use client";

import { RotateCw, Sparkles, TriangleAlert } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import { useEffect, useRef, useState } from "react";

import { notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { sendAnswerPrompt } from "@/lib/chat-queries";
import { baseName, type ViewedFile } from "@/lib/outputs";
import { cn } from "@/lib/utils";

import {
  ANSWER_BRIDGE,
  ANSWER_HEIGHT,
  type AnswerContext,
  AnswerError,
  type AnswerHost,
  type AnswerTheme,
  answerReply,
  answerRequest,
  PromptLimiter,
  readAnswerRequest,
} from "./answer-bridge";
import { useChatFailure } from "./hooks";

/** How far from the viewport a message is when its answer starts to load. */
const NEAR = "600px 0px";

type Phase = "idle" | "loading" | "ready" | "error";

/**
 * The nearest ancestor of `element` that scrolls (the thread's column), or null for the viewport. The observer must
 * take it as its root: with the viewport as root, the column's own clipping hides a message until it is on screen,
 * and NEAR would never reach past it.
 */
function scrollingAncestor(element: HTMLElement): HTMLElement | null {
  for (let parent = element.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
    if (/(auto|scroll|overlay)/.test(getComputedStyle(parent).overflowY)) return parent;
  }
  return null;
}


/**
 * An interactive answer inside its chat message (web/DESIGN.md, Chats): the page the agent wrote, in a frame with
 * `sandbox="allow-scripts"` and no other token (never the one that keeps the origin), so it runs with an opaque
 * origin, reads no cookie or storage of the hub and cannot reach the page around it. The hub serves it under a CSP of the same sandbox with connect-src
 * 'none' (docs/hub.md, Interactive answers). Its one way out is the bridge evo-answer/1 (`answer-bridge.ts`): a message
 * from this frame's window, answered here. The frame loads only once its message comes within NEAR of the viewport;
 * before the page answers it shows a skeleton, and a refused or failed read shows an error with Try again.
 */
export function InteractiveAnswer({ chatId, output, className }: { chatId: number; output: ViewedFile; className?: string }) {
  const t = useTranslations("chats.answer");
  const chatFailure = useChatFailure();
  const locale = useLocale();
  const { resolvedTheme } = useTheme();
  const theme: AnswerTheme = resolvedTheme === "dark" ? "dark" : "light";
  const box = useRef<HTMLElement>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  const [near, setNear] = useState(false);
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [height, setHeight] = useState(ANSWER_HEIGHT.initial);
  const name = output.caption?.trim() || baseName(output.name);
  const phase: Phase = failed ? "error" : ready ? "ready" : near ? "loading" : "idle";

  // What the handler reads when a message arrives, kept current without adding and removing the listener each render.
  const latest = useRef({ theme, locale, t, chatFailure });
  useEffect(() => {
    latest.current = { theme, locale, t, chatFailure };
  });

  useEffect(() => {
    const element = box.current;
    if (!element || near) return;
    if (typeof IntersectionObserver === "undefined") {
      queueMicrotask(() => setNear(true));
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) setNear(true);
      },
      { root: scrollingAncestor(element), rootMargin: NEAR },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [near]);

  useEffect(() => {
    const element = frame.current;
    if (!near || !element) return;
    const limiter = new PromptLimiter();
    const host: AnswerHost = {
      context: (): AnswerContext => ({ bridge: ANSWER_BRIDGE, theme: latest.current.theme, locale: latest.current.locale }),
      resize: setHeight,
      limiter,
      openLink: (url) => {
        window.open(url, "_blank", "noopener,noreferrer");
      },
      sendPrompt: async (text) => {
        try {
          const message = await sendAnswerPrompt(browserApi(), chatId, output.id, text);
          return { message_id: message.id };
        } catch (error) {
          notifyFailure(latest.current.t("promptFailed"), latest.current.chatFailure(error));
          if (isApiError(error) && error.status === 429) throw new AnswerError("rate_limited", error.info.message, 429);
          if (isApiError(error) && error.status >= 400 && error.status < 500) {
            throw new AnswerError("hub_refused", error.info.message, error.status);
          }
          throw new AnswerError("hub_unavailable", "the hub did not answer; try again shortly");
        }
      },
    };
    const post = (message: unknown) => element.contentWindow?.postMessage(message, "*");
    const onMessage = (event: MessageEvent) => {
      // Only this frame's window, holding a document of an opaque origin: anything else is not the answer's page.
      if (event.source === null || event.source !== element.contentWindow || event.origin !== "null") return;
      const request = readAnswerRequest(event.data);
      if (request === null) return;
      if ("error" in request) {
        post(answerReply(request.id, { error: request.error }));
        return;
      }
      answerRequest(request, host)
        .then((result) => post(answerReply(request.id, { result })))
        .catch((error: unknown) => post(answerReply(request.id, { error })));
    };
    window.addEventListener("message", onMessage);

    // The hub's refusal (the message gone, the reader's access lost) shows as an error, not as its JSON in the frame.
    // The page loads only once the read passed and the bridge listens, so its first requests are answered.
    const abort = new AbortController();
    fetch(output.url, { credentials: "same-origin", signal: abort.signal })
      .then(async (response) => {
        await response.body?.cancel();
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        element.setAttribute("src", output.url);
      })
      .catch(() => {
        if (!abort.signal.aborted) setFailed(true);
      });
    return () => {
      abort.abort();
      window.removeEventListener("message", onMessage);
      element.removeAttribute("src");
    };
  }, [near, attempt, chatId, output.id, output.url]);

  useEffect(() => {
    frame.current?.contentWindow?.postMessage({ bridge: ANSWER_BRIDGE, event: "theme", theme }, "*");
  }, [theme]);

  return (
    <figure
      ref={box}
      className={cn("m-0 flex w-full min-w-0 flex-col gap-1", className)}
      data-testid="interactive-answer"
      data-output-id={output.id}
      data-state={phase}
    >
      <div className="w-full overflow-hidden rounded-lg border bg-card">
        {phase === "error" ? (
          <div role="alert" className="flex flex-wrap items-center gap-2 px-3 py-3 text-sm text-foreground" data-testid="interactive-answer-error">
            <TriangleAlert className="size-4 shrink-0 text-destructive" aria-hidden="true" />
            <span className="min-w-0 flex-1">{t("failed")}</span>
            <Button type="button" variant="outline" size="sm" onClick={() => {
                setFailed(false);
                setReady(false);
                setAttempt((count) => count + 1);
              }} data-testid="interactive-answer-retry">
              <RotateCw aria-hidden="true" />
              {t("retry")}
            </Button>
          </div>
        ) : (
          // The height the page asked is the frame's own; the border sits outside it.
          <div className="relative w-full" style={{ height }}>
            {near ? (
              <iframe
                key={attempt}
                ref={frame}
                title={t("frameTitle", { name })}
                sandbox="allow-scripts"
                referrerPolicy="no-referrer"
                onLoad={() => {
                  if (frame.current?.getAttribute("src")) setReady(true);
                }}
                className="block size-full border-0 bg-transparent"
                data-testid="interactive-answer-frame"
              />
            ) : null}
            {phase !== "ready" ? (
              <div className="absolute inset-0 flex flex-col gap-2 bg-card p-3" aria-hidden="true" data-testid="interactive-answer-loading">
                <Skeleton className="h-4 w-2/5" />
                <Skeleton className="h-3 w-4/5" />
                <Skeleton className="min-h-0 w-full flex-1" />
              </div>
            ) : null}
          </div>
        )}
      </div>
      <figcaption className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
        <Sparkles className="size-3.5 shrink-0" aria-hidden="true" />
        <span className="min-w-0 truncate">{t("caption", { name })}</span>
        {phase === "loading" ? <span className="sr-only" role="status">{t("loading")}</span> : null}
      </figcaption>
    </figure>
  );
}
