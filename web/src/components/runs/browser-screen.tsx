"use client";

import { type ReactNode, type Ref, useEffect, useImperativeHandle, useRef, useState } from "react";

import { useModifierKey } from "@/lib/keyboard";
import { cn } from "@/lib/utils";

import {
  type BrowserEvent,
  buttonOf,
  fitted,
  type KeyInput,
  keyEventOf,
  modifiersOf,
  pasteChunks,
  type ScreenHeader,
  toPage,
  wheelDelta,
} from "./browser-view-model";

/**
 * The picture of the run's browser, drawn as large as its box allows, and in human mode the person's hands on it. A
 * mouse or pen is the page's mouse; a touch is too: a tap clicks and a drag scrolls. Keys go to a text box hidden
 * over the picture, which takes the focus on a click (or from the Keyboard button, which opens a phone's keyboard):
 * a key is sent as a key, so the app's own shortcuts stay quiet while the person drives; what an input method
 * composes (Telex, VNI, a phone's keyboard) goes as text when it is done, and a paste as text. Escape then Tab leaves.
 */
export type ScreenHandle = {
  draw: (header: ScreenHeader, jpeg: Uint8Array) => Promise<void>;
  /** Put the keys in the page: on a touch screen, open its keyboard. */
  focusKeys: () => void;
};

/** Two presses this close in time (ms) and place (px) make a double click. */
const MULTI_CLICK_MS = 500;
const MULTI_CLICK_PX = 6;
/** A touch that moves this far (px) scrolls rather than clicks. */
const DRAG_PX = 8;

type Touch = { id: number; x: number; y: number; lastX: number; lastY: number; dragging: boolean };

function keyOf(name: "Backspace" | "Delete" | "Enter", action: "down" | "up"): KeyInput {
  const keyCode = { Backspace: 8, Delete: 46, Enter: 13 }[name];
  return { type: "key", action, key: name, code: name, keyCode, text: name === "Enter" && action === "down" ? "\r" : "", modifiers: 0 };
}

export function BrowserScreen({
  ref,
  human,
  send,
  label,
  keysLabel,
  describedBy,
  children,
  onFrame,
}: {
  ref: Ref<ScreenHandle>;
  /** The person drives the browser: input goes to it. */
  human: boolean;
  send: (event: BrowserEvent) => boolean;
  label: string;
  keysLabel: string;
  describedBy: string;
  /** Laid over the picture: what the view waits for. */
  children?: ReactNode;
  onFrame: () => void;
}) {
  const apple = useModifierKey() === "meta";
  const boxRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const keysRef = useRef<HTMLTextAreaElement>(null);
  const headerRef = useRef<ScreenHeader | null>(null);
  const handlers = useRef({ send, onFrame, apple, human });
  useEffect(() => {
    handlers.current = { send, onFrame, apple, human };
  });
  const [composing, setComposing] = useState<string | null>(null);

  useImperativeHandle(
    ref,
    () => ({
      draw: async (header, jpeg) => {
        const bitmap = await createImageBitmap(new Blob([jpeg as Uint8Array<ArrayBuffer>], { type: "image/jpeg" }));
        try {
          const canvas = canvasRef.current;
          const context = canvas?.getContext("2d");
          if (!canvas || !context) return;
          if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
            canvas.width = bitmap.width;
            canvas.height = bitmap.height;
          }
          context.drawImage(bitmap, 0, 0);
          headerRef.current = header;
          handlers.current.onFrame();
        } finally {
          bitmap.close();
        }
      },
      focusKeys: () => keysRef.current?.focus({ preventScroll: true }),
    }),
    [],
  );

  // Pointer, wheel and keys, as listeners of their own: the wheel's must be able to stop the page scrolling, and the
  // keys' must stop the app's shortcuts (listeners on the document) from hearing them.
  useEffect(() => {
    const box = boxRef.current;
    const canvas = canvasRef.current;
    if (!box || !canvas || !human) return;
    const out = (event: BrowserEvent) => handlers.current.send(event);

    const shown = () => fitted(canvas.getBoundingClientRect(), canvas.width, canvas.height);
    const pointAt = (x: number, y: number, clampToPage = false) => {
      const header = headerRef.current;
      return header ? toPage(x, y, shown(), header, clampToPage) : null;
    };
    const modifiers = (event: MouseEvent) => modifiersOf(event, handlers.current.apple);

    let last = { at: 0, x: 0, y: 0, button: -1, clicks: 0 };
    const clicksAt = (event: PointerEvent) => {
      const near = Math.abs(event.clientX - last.x) <= MULTI_CLICK_PX && Math.abs(event.clientY - last.y) <= MULTI_CLICK_PX;
      const clicks = near && event.button === last.button && event.timeStamp - last.at <= MULTI_CLICK_MS ? Math.min(last.clicks + 1, 3) : 1;
      last = { at: event.timeStamp, x: event.clientX, y: event.clientY, button: event.button, clicks };
      return clicks;
    };

    let frame = 0;
    let move: { x: number; y: number; buttons: number; modifiers: number } | null = null;
    let wheel: { x: number; y: number; dx: number; dy: number; modifiers: number } | null = null;
    const flush = () => {
      frame = 0;
      if (move) out({ type: "mouse", action: "move", ...move, button: "none", clicks: 0 });
      if (wheel) out({ type: "wheel", ...wheel });
      move = null;
      wheel = null;
    };
    const later = () => {
      if (frame === 0) frame = requestAnimationFrame(flush);
    };

    let touch: Touch | null = null;
    let pressed = 0; // mouse buttons held, from the person's down to their up

    const onPointerDown = (event: PointerEvent) => {
      if (event.pointerType === "touch") {
        if (touch !== null) return; // one finger drives; a second is ignored
        event.preventDefault();
        touch = { id: event.pointerId, x: event.clientX, y: event.clientY, lastX: event.clientX, lastY: event.clientY, dragging: false };
        box.setPointerCapture(event.pointerId);
        return;
      }
      const point = pointAt(event.clientX, event.clientY);
      if (!point) return;
      event.preventDefault(); // the focus stays in the text box, which takes it now
      keysRef.current?.focus({ preventScroll: true });
      box.setPointerCapture(event.pointerId);
      pressed |= 1 << event.button;
      flush();
      out({ type: "mouse", action: "down", ...point, button: buttonOf(event.button), buttons: event.buttons & 31, clicks: clicksAt(event), modifiers: modifiers(event) });
    };
    const onPointerMove = (event: PointerEvent) => {
      if (event.pointerType === "touch") {
        if (!touch || touch.id !== event.pointerId) return;
        const far = Math.hypot(event.clientX - touch.x, event.clientY - touch.y) > DRAG_PX;
        if (!touch.dragging && !far) return;
        touch.dragging = true;
        const header = headerRef.current;
        const picture = shown();
        const at = pointAt(touch.x, touch.y, true);
        if (header && at && picture.width > 0) {
          const scale = header.width / picture.width; // page pixels a screen pixel covers
          const dx = (touch.lastX - event.clientX) * scale;
          const dy = (touch.lastY - event.clientY) * scale;
          wheel = { ...at, dx: (wheel?.dx ?? 0) + dx, dy: (wheel?.dy ?? 0) + dy, modifiers: 0 };
          later();
        }
        touch.lastX = event.clientX;
        touch.lastY = event.clientY;
        return;
      }
      const point = pointAt(event.clientX, event.clientY, pressed !== 0);
      if (!point) return;
      move = { ...point, buttons: event.buttons & 31, modifiers: modifiers(event) };
      later();
    };
    const onPointerUp = (event: PointerEvent) => {
      if (event.pointerType === "touch") {
        if (!touch || touch.id !== event.pointerId) return;
        const tap = !touch.dragging;
        const point = pointAt(touch.x, touch.y);
        touch = null;
        flush();
        if (tap && point) {
          const clicks = clicksAt(event);
          out({ type: "mouse", action: "move", ...point, button: "none", buttons: 0, clicks: 0, modifiers: 0 });
          out({ type: "mouse", action: "down", ...point, button: "left", buttons: 1, clicks, modifiers: 0 });
          out({ type: "mouse", action: "up", ...point, button: "left", buttons: 0, clicks, modifiers: 0 });
        }
        return;
      }
      if (!(pressed & (1 << event.button))) return;
      pressed &= ~(1 << event.button);
      const point = pointAt(event.clientX, event.clientY, true);
      flush();
      if (point) out({ type: "mouse", action: "up", ...point, button: buttonOf(event.button), buttons: event.buttons & 31, clicks: last.clicks || 1, modifiers: modifiers(event) });
    };
    const onPointerCancel = (event: PointerEvent) => {
      if (touch?.id === event.pointerId) touch = null;
    };
    const onWheel = (event: WheelEvent) => {
      const point = pointAt(event.clientX, event.clientY, true);
      if (!point) return;
      event.preventDefault();
      const height = headerRef.current?.height ?? 800;
      wheel = {
        ...point,
        dx: (wheel?.dx ?? 0) + wheelDelta(event.deltaX, event.deltaMode, height),
        dy: (wheel?.dy ?? 0) + wheelDelta(event.deltaY, event.deltaMode, height),
        modifiers: modifiers(event),
      };
      later();
    };
    const onContextMenu = (event: Event) => event.preventDefault(); // the right button is the page's

    box.addEventListener("pointerdown", onPointerDown);
    box.addEventListener("pointermove", onPointerMove);
    box.addEventListener("pointerup", onPointerUp);
    box.addEventListener("pointercancel", onPointerCancel);
    box.addEventListener("wheel", onWheel, { passive: false });
    box.addEventListener("contextmenu", onContextMenu);
    return () => {
      cancelAnimationFrame(frame);
      box.removeEventListener("pointerdown", onPointerDown);
      box.removeEventListener("pointermove", onPointerMove);
      box.removeEventListener("pointerup", onPointerUp);
      box.removeEventListener("pointercancel", onPointerCancel);
      box.removeEventListener("wheel", onWheel);
      box.removeEventListener("contextmenu", onContextMenu);
    };
  }, [human]);

  useEffect(() => {
    const keys = keysRef.current;
    if (!keys || !human) return;
    const out = (event: BrowserEvent) => handlers.current.send(event);
    const down = new Map<string, KeyInput>(); // keys sent down, to send up
    let lastKey = "";
    let composingNow = false;
    const clear = () => {
      if (!composingNow) keys.value = "";
    };

    const onKeyDown = (event: KeyboardEvent) => {
      const leaving = event.key === "Tab" && lastKey === "Escape";
      lastKey = event.key;
      if (leaving) return; // the browser moves the focus on, out of the view
      event.stopPropagation(); // the app's shortcuts are the page's keys now
      const key = keyEventOf(event, "down", handlers.current.apple);
      if (!key) return; // an input method or a paste: the text box takes it, and its events send the text
      event.preventDefault();
      if (out(key)) down.set(event.code || event.key, key);
    };
    const onKeyUp = (event: KeyboardEvent) => {
      event.stopPropagation();
      const id = event.code || event.key;
      const pressed = down.get(id);
      if (!pressed) return;
      down.delete(id);
      event.preventDefault();
      out({ ...pressed, action: "up", text: "", modifiers: keyEventOf(event, "up", handlers.current.apple)?.modifiers ?? pressed.modifiers });
    };
    const onBlur = () => {
      for (const pressed of down.values()) out({ ...pressed, action: "up", text: "" });
      down.clear();
    };
    const onCompositionStart = () => {
      composingNow = true;
      setComposing("");
    };
    const onCompositionUpdate = (event: CompositionEvent) => setComposing(event.data);
    const onCompositionEnd = (event: CompositionEvent) => {
      composingNow = false;
      setComposing(null);
      for (const text of pasteChunks(event.data ?? "")) out({ type: "text", text });
      setTimeout(clear, 0); // after the input event that may follow
    };
    const onInput = (event: Event) => {
      const input = event as InputEvent;
      if (composingNow || input.isComposing) return;
      const text = input.data ?? input.dataTransfer?.getData("text/plain") ?? "";
      switch (input.inputType) {
        case "insertText":
        case "insertReplacementText":
          for (const chunk of pasteChunks(text)) out({ type: "text", text: chunk });
          break;
        case "deleteContentBackward":
        case "deleteWordBackward":
          out(keyOf("Backspace", "down"));
          out(keyOf("Backspace", "up"));
          break;
        case "deleteContentForward":
          out(keyOf("Delete", "down"));
          out(keyOf("Delete", "up"));
          break;
        case "insertLineBreak":
        case "insertParagraph":
          out(keyOf("Enter", "down"));
          out(keyOf("Enter", "up"));
          break;
        default:
          break; // insertFromComposition and the like: compositionend sent it
      }
      clear();
    };
    const onPaste = (event: ClipboardEvent) => {
      event.preventDefault();
      event.stopPropagation();
      for (const text of pasteChunks(event.clipboardData?.getData("text/plain") ?? "")) out({ type: "paste", text });
    };

    keys.addEventListener("keydown", onKeyDown);
    keys.addEventListener("keyup", onKeyUp);
    keys.addEventListener("blur", onBlur);
    keys.addEventListener("compositionstart", onCompositionStart);
    keys.addEventListener("compositionupdate", onCompositionUpdate);
    keys.addEventListener("compositionend", onCompositionEnd);
    keys.addEventListener("input", onInput);
    keys.addEventListener("paste", onPaste);
    return () => {
      onBlur();
      keys.removeEventListener("keydown", onKeyDown);
      keys.removeEventListener("keyup", onKeyUp);
      keys.removeEventListener("blur", onBlur);
      keys.removeEventListener("compositionstart", onCompositionStart);
      keys.removeEventListener("compositionupdate", onCompositionUpdate);
      keys.removeEventListener("compositionend", onCompositionEnd);
      keys.removeEventListener("input", onInput);
      keys.removeEventListener("paste", onPaste);
      setComposing(null);
    };
  }, [human]);

  return (
    <div
      ref={boxRef}
      className={cn(
        "relative h-full w-full overflow-hidden outline-none select-none has-[textarea:focus-visible]:outline-2 has-[textarea:focus-visible]:-outline-offset-2 has-[textarea:focus-visible]:outline-ring",
        human ? "cursor-default touch-none" : "touch-auto",
      )}
      data-testid="browser-screen"
      data-human={human || undefined}
    >
      <canvas ref={canvasRef} width={0} height={0} role="img" aria-label={label} className="block h-full w-full object-contain" data-testid="browser-canvas" />
      {human ? (
        <textarea
          ref={keysRef}
          aria-label={keysLabel}
          aria-describedby={describedBy}
          autoComplete="off"
          autoCorrect="off"
          autoCapitalize="off"
          spellCheck={false}
          rows={1}
          className="pointer-events-none absolute bottom-0 left-0 h-px w-px resize-none overflow-hidden border-0 bg-transparent p-0 text-base text-transparent caret-transparent opacity-0"
          data-testid="browser-keys"
        />
      ) : null}
      {composing ? (
        <span aria-hidden="true" className="absolute bottom-3 left-3 max-w-[calc(100%-1.5rem)] truncate rounded-sm border bg-popover px-2 py-1 text-sm text-popover-foreground underline shadow-sm" data-testid="browser-composing">
          {composing}
        </span>
      ) : null}
      {children}
    </div>
  );
}
