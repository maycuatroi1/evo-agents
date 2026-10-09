"use client";

import { useQuery } from "@tanstack/react-query";
import { Keyboard, X } from "lucide-react";
import type { Route } from "next";
import dynamic from "next/dynamic";
import { usePathname, useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { createContext, Fragment, type ReactNode, useCallback, useContext, useEffect, useId, useMemo, useRef, useState } from "react";

import { notify } from "@/components/feedback/toast";
import { useCanDispatch, useDispatchedToast } from "@/components/runs/hooks";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/kbd";
import { Switch } from "@/components/ui/switch";
import { browserApi } from "@/lib/api/browser";
import {
  characterKeyBlocked,
  comboText,
  type KeyPart,
  keyText,
  type ModifierKey,
  setCharacterKeys,
  useCharacterKeys,
  useModifierKey,
} from "@/lib/keyboard";
import { whoamiQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { type NavLabel, projectHref } from "./nav";
import { useCurrentProject } from "./project-switcher";

/**
 * The hub's keyboard shortcuts in one registry, and the shell's own keys. `SHORTCUTS` lists every key the web answers
 * to, for the shortcuts dialog (`?`) and for the `kbd` shown beside each action. Most keys are handled here: G then H,
 * I, P, R, C or W (within a second) go to Home, Inbox, the project's Plans, Runs and Curator, and Workers; D opens Dispatch on a
 * project's pages for a writer; ? opens the dialog. The others stay with what they act on: Cmd K or Ctrl K in
 * palette/palette-context.tsx, `/` in data/search-shortcut.ts, Cmd or Ctrl with Enter in the answer and message
 * forms, Cmd or Ctrl with B in ui/sidebar.tsx, Esc in each dialog.
 *
 * Single keys never take a key pressed with Cmd, Ctrl or Alt (the browser's and the screen reader's own), nor one
 * typed in a text field, the web terminal, a menu or a dialog (`characterKeyBlocked` in lib/keyboard.ts). The visitor
 * can turn them off in the dialog (WCAG 2.1.4); the shortcuts with Cmd or Ctrl stay.
 */

export type ShortcutId =
  | "palette"
  | "search"
  | "help"
  | "sidebar"
  | "close"
  | "goHome"
  | "goInbox"
  | "goPlans"
  | "goRuns"
  | "goCurator"
  | "goWorkers"
  | "dispatch"
  | "send";

type ShortcutGroup = "general" | "goto" | "actions";

export type Shortcut = {
  id: ShortcutId;
  group: ShortcutGroup;
  /** Keys pressed together; a second entry is pressed after the first (G then H). */
  keys: readonly (readonly KeyPart[])[];
  /** A key pressed alone: off while the visitor has single-key shortcuts off. */
  single: boolean;
  /** Where it works, when that is not every page: said under its name in the dialog. */
  where?: "project" | "writer";
};

export const SHORTCUTS: readonly Shortcut[] = [
  { id: "palette", group: "general", keys: [["mod", "k"]], single: false },
  { id: "search", group: "general", keys: [["/"]], single: true },
  { id: "help", group: "general", keys: [["?"]], single: true },
  { id: "sidebar", group: "general", keys: [["mod", "b"]], single: false },
  { id: "close", group: "general", keys: [["esc"]], single: false },
  { id: "dispatch", group: "actions", keys: [["d"]], single: true, where: "writer" },
  { id: "send", group: "actions", keys: [["mod", "enter"]], single: false },
  { id: "goHome", group: "goto", keys: [["g"], ["h"]], single: true },
  { id: "goInbox", group: "goto", keys: [["g"], ["i"]], single: true },
  { id: "goPlans", group: "goto", keys: [["g"], ["p"]], single: true, where: "project" },
  { id: "goRuns", group: "goto", keys: [["g"], ["r"]], single: true, where: "project" },
  { id: "goCurator", group: "goto", keys: [["g"], ["c"]], single: true, where: "project" },
  { id: "goWorkers", group: "goto", keys: [["g"], ["w"]], single: true },
];

const BY_ID = Object.fromEntries(SHORTCUTS.map((entry) => [entry.id, entry])) as Record<ShortcutId, Shortcut>;

/** The dialog's groups, in two columns from 768 px: General and Actions, then Go to. */
const COLUMNS: readonly (readonly ShortcutGroup[])[] = [["general", "actions"], ["goto"]];

/** The keys of a shortcut as one `kbd` each stroke: "⌘K" or "Ctrl K"; "G", "H". */
function strokeTexts(id: ShortcutId, modifier: ModifierKey | null): string[] {
  return BY_ID[id].keys.map((stroke) => comboText(stroke, modifier));
}

/** A chord's keys as the one `kbd` beside its action shows them: "⌘K" on Apple devices, "Ctrl K" elsewhere. */
export function shortcutText(id: ShortcutId, modifier: ModifierKey | null): string {
  return strokeTexts(id, modifier).join(" ");
}

/** The key that starts a "go to" sequence, and how long the second key may take. */
const LEAD = "g";
export const LEAD_MS = 1_000;
/** How long after G the hint of the second keys shows, so a quick G H does not flash it. */
const HINT_MS = 400;

type GoKey = "h" | "i" | "p" | "r" | "c" | "w";

/** Where each second key goes: a hub page, or a page of the project shown (or the visitor's only project). */
const GO: Record<GoKey, { id: ShortcutId; label: NavLabel } & ({ href: Route } | { segment: string })> = {
  h: { id: "goHome", label: "home", href: "/" },
  i: { id: "goInbox", label: "inbox", href: "/inbox" },
  p: { id: "goPlans", label: "plans", segment: "plans" },
  r: { id: "goRuns", label: "runs", segment: "runs" },
  c: { id: "goCurator", label: "curator", segment: "curator" },
  w: { id: "goWorkers", label: "workers", href: "/workers" },
};

/** The sidebar's items that a "go to" sequence opens, for the `kbd` beside each. */
export const NAV_SHORTCUTS: Partial<Record<NavLabel, ShortcutId>> = Object.fromEntries(
  Object.values(GO).map((target) => [target.label, target.id]),
);

function isGoKey(letter: string): letter is GoKey {
  return letter in GO;
}

/** The letter on the key: the character typed, or on a layout without Latin letters the key's place (KeyG). */
function letterOf(event: Pick<KeyboardEvent, "key" | "code">): string | null {
  if (/^[a-z]$/i.test(event.key)) return event.key.toLowerCase();
  if (event.key.length === 1 && /^Key[A-Z]$/.test(event.code ?? "")) return event.code.slice(3).toLowerCase();
  return null;
}

export type KeyAction = { kind: "lead" } | { kind: "go"; key: GoKey } | { kind: "dispatch" } | { kind: "help" } | null;

type KeyEvent = Pick<
  KeyboardEvent,
  "key" | "code" | "shiftKey" | "defaultPrevented" | "isComposing" | "repeat" | "ctrlKey" | "metaKey" | "altKey" | "target"
>;

/**
 * What a key press asks of the shell, `afterLead` being whether G was pressed less than a second ago: nothing when
 * the key belongs to what has focus (`characterKeyBlocked`), `?` with or without Shift (layouts differ), and the
 * letters without Shift.
 */
export function readKey(event: KeyEvent, afterLead: boolean): KeyAction {
  if (characterKeyBlocked(event)) return null;
  if (event.key === "?") return { kind: "help" };
  if (event.shiftKey) return null;
  const letter = letterOf(event);
  if (letter === null) return null;
  if (afterLead && isGoKey(letter)) return { kind: "go", key: letter };
  if (letter === LEAD) return { kind: "lead" };
  if (letter === "d") return { kind: "dispatch" };
  return null;
}

type ShortcutsState = {
  /** Opens the shortcuts dialog; focus goes back to `from` (by default what holds focus now) when it closes. */
  openShortcuts: (from?: HTMLElement | null) => void;
  /** A page's own Dispatch, which D opens instead of the shell's; returns its removal. */
  registerDispatch: (open: () => void) => () => void;
};

const ShortcutsContext = createContext<ShortcutsState | null>(null);

/** The shortcuts dialog's opener, or null outside the shell (a component rendered on its own in a test). */
export function useOpenShortcuts(): ShortcutsState["openShortcuts"] | null {
  return useContext(ShortcutsContext)?.openShortcuts ?? null;
}

/**
 * Lets D open the page's own Dispatch (its dialog, with the step of the page picked) while `open` is set. Returns
 * whether D does so now, for the page to show the key beside its button: in the shell, with single keys on.
 */
export function useDispatchShortcut(open: (() => void) | null): boolean {
  const context = useContext(ShortcutsContext);
  const characterKeys = useCharacterKeys();
  const latest = useRef(open);
  useEffect(() => {
    latest.current = open;
  });
  const register = context?.registerDispatch;
  const enabled = open !== null && register !== undefined;
  useEffect(() => {
    if (!enabled || !register) return;
    return register(() => latest.current?.());
  }, [enabled, register]);
  return enabled && characterKeys;
}

const LazyDispatchDialog = dynamic(() => import("@/components/runs/dispatch-dialog").then((module) => module.DispatchDialog), {
  ssr: false,
});

/**
 * The shell's keys and what they open: the shortcuts dialog, the hint after G, and Dispatch for a page that has no
 * Dispatch of its own (the dialog's code is fetched at the first D).
 */
export function ShortcutsProvider({ children }: { children: ReactNode }) {
  const t = useTranslations("shortcuts");
  const router = useRouter();
  const pathname = usePathname();
  const project = useCurrentProject();
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const canDispatch = useCanDispatch(project ?? "");
  const dispatched = useDispatchedToast();

  const [helpOpen, setHelpOpen] = useState(false);
  const [hint, setHint] = useState(false);
  const [dispatchIn, setDispatchIn] = useState<string | null>(null);
  const [dispatchOpen, setDispatchOpen] = useState(false);
  const opener = useRef<HTMLElement | null>(null);
  const pageDispatch = useRef<(() => void)[]>([]);
  const lead = useRef<{ end: ReturnType<typeof setTimeout>; hint: ReturnType<typeof setTimeout> } | null>(null);

  // What a key press reads; kept current without adding the listener again, so a pending G survives a render.
  const now = useRef({ project, canDispatch, grants: me?.grants ?? [], pathname, router, t });
  useEffect(() => {
    now.current = { project, canDispatch, grants: me?.grants ?? [], pathname, router, t };
  });

  const remember = useCallback((from?: HTMLElement | null) => {
    const active = document.activeElement;
    opener.current = from ?? (active instanceof HTMLElement && active !== document.body ? active : null);
  }, []);

  /** A dialog the keyboard opened has no trigger: focus goes back to what held it, if it is still on the page. */
  const giveFocusBack = useCallback((event: Event) => {
    event.preventDefault();
    const target = opener.current;
    opener.current = null;
    if (target?.isConnected) target.focus();
  }, []);

  const openShortcuts = useCallback(
    (from?: HTMLElement | null) => {
      remember(from);
      setHelpOpen(true);
    },
    [remember],
  );

  const registerDispatch = useCallback((open: () => void) => {
    pageDispatch.current = [...pageDispatch.current, open];
    return () => {
      pageDispatch.current = pageDispatch.current.filter((other) => other !== open);
    };
  }, []);

  useEffect(() => {
    const endLead = () => {
      if (lead.current) {
        clearTimeout(lead.current.end);
        clearTimeout(lead.current.hint);
        lead.current = null;
      }
      setHint(false);
    };

    const go = (key: GoKey) => {
      const target = GO[key];
      const { grants, pathname: here, router, t } = now.current;
      if ("href" in target) {
        if (target.href !== here) router.push(target.href);
        return;
      }
      // Plans, Runs and the Curator are a project's: the one shown, or the visitor's only one.
      const project = now.current.project ?? (grants.length === 1 ? grants[0].project : null);
      if (project === null) {
        notify({ id: "shortcuts-need-project", tone: "info", text: t("needProject.title"), description: t("needProject.text") });
        return;
      }
      const href = projectHref(project, target.segment);
      if (href !== here) router.push(href);
    };

    const onKeyDown = (event: KeyboardEvent) => {
      const action = readKey(event, lead.current !== null);
      if (action === null) {
        // A key that is no second key ends the sequence; a lone modifier (Shift on the way to "?") does not.
        if (lead.current && !["Shift", "CapsLock"].includes(event.key)) endLead();
        return;
      }
      endLead();
      if (action.kind === "lead") {
        event.preventDefault();
        lead.current = { end: setTimeout(endLead, LEAD_MS), hint: setTimeout(() => setHint(true), HINT_MS) };
      } else if (action.kind === "go") {
        event.preventDefault();
        go(action.key);
      } else if (action.kind === "help") {
        event.preventDefault();
        openShortcuts();
      } else {
        const { project, canDispatch } = now.current;
        if (project === null || !canDispatch) return; // not a writer's project page: the key stays the page's
        event.preventDefault();
        const page = pageDispatch.current.at(-1);
        if (page) {
          page();
          return;
        }
        remember();
        setDispatchIn(project);
        setDispatchOpen(true);
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      endLead();
    };
  }, [openShortcuts, remember]);

  const value = useMemo(() => ({ openShortcuts, registerDispatch }), [openShortcuts, registerDispatch]);
  return (
    <ShortcutsContext.Provider value={value}>
      {children}
      <ShortcutsDialog open={helpOpen} onOpenChange={setHelpOpen} onCloseAutoFocus={giveFocusBack} />
      {hint ? <LeadHint /> : null}
      {dispatchIn !== null ? (
        <LazyDispatchDialog
          key={dispatchIn}
          project={dispatchIn}
          open={dispatchOpen}
          onOpenChange={setDispatchOpen}
          onDispatched={dispatched}
          onCloseAutoFocus={giveFocusBack}
        />
      ) : null}
    </ShortcutsContext.Provider>
  );
}

/**
 * The keys of a shortcut beside the action they trigger, for the eye: the action names them with
 * `aria-keyshortcuts` where ARIA can say them, and the dialog lists them all in words. Hidden under 768 px, where a
 * keyboard is rare, while single keys are off, and until the page knows whether the keyboard says ⌘ or Ctrl.
 */
export function ShortcutKeys({ id, className, keyClassName }: { id: ShortcutId; className?: string; keyClassName?: string }) {
  const characterKeys = useCharacterKeys();
  const modifier = useModifierKey();
  const entry = BY_ID[id];
  if (entry.single ? !characterKeys : modifier === null) return null;
  return (
    <span aria-hidden="true" className={cn("inline-flex shrink-0 items-center gap-0.5 max-md:hidden", className)} data-shortcut={id}>
      {strokeTexts(id, modifier).map((text, index) => (
        <Kbd key={index} className={keyClassName}>
          {text}
        </Kbd>
      ))}
    </span>
  );
}

/** A key in words, for screen readers: "Control", "Slash", "Question mark"; a letter as it is. */
function useSpokenKey() {
  const t = useTranslations("shortcuts.keys");
  return (part: KeyPart, modifier: ModifierKey): string => {
    if (part === "mod") return modifier === "meta" ? t("command") : t("control");
    if (part === "enter") return t("enter");
    if (part === "esc") return t("escape");
    if (part === "/") return t("slash");
    if (part === "?") return t("question");
    return part.toUpperCase();
  };
}

/** A shortcut's keys in the dialog: one `kbd` per key, "then" between the strokes of a sequence, and all of it in words. */
function KeySequence({ keys, modifier }: { keys: Shortcut["keys"]; modifier: ModifierKey }) {
  const t = useTranslations("shortcuts");
  const spoken = useSpokenKey();
  const words = keys.map((stroke) => stroke.map((part) => spoken(part, modifier)).join(" ")).join(` ${t("then")} `);
  return (
    <>
      <span className="sr-only">{words}</span>
      <span aria-hidden="true" className="flex items-center gap-1.5">
        {keys.map((stroke, index) => (
          <Fragment key={index}>
            {index > 0 ? <span className="text-xs text-fg-subtle">{t("then")}</span> : null}
            <span className="flex items-center gap-0.5">
              {stroke.map((part) => (
                <Kbd key={part}>{keyText(part, modifier)}</Kbd>
              ))}
            </span>
          </Fragment>
        ))}
      </span>
    </>
  );
}

/**
 * The shortcuts dialog: every key in the registry by group, each in a row with what it does, where it works when not
 * everywhere, and its keys; at the foot, the switch that turns single keys off on this browser.
 */
function ShortcutsDialog({
  open,
  onOpenChange,
  onCloseAutoFocus,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCloseAutoFocus: (event: Event) => void;
}) {
  const t = useTranslations("shortcuts");
  const ids = useId();
  const characterKeys = useCharacterKeys();
  const modifier = useModifierKey() ?? "ctrl";
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        showCloseButton={false}
        className="flex max-h-[calc(100dvh-2rem)] flex-col gap-0 overflow-hidden p-0 sm:max-w-lg md:max-w-3xl"
        onCloseAutoFocus={onCloseAutoFocus}
        data-testid="shortcuts-dialog"
      >
        <div className="flex items-start gap-3 border-b px-4 pt-4 pb-3 sm:px-5">
          <div className="flex min-w-0 flex-1 flex-col gap-2">
            <DialogTitle className="flex items-center gap-2 text-lg font-semibold">
              <Keyboard className="size-5 text-muted-foreground" aria-hidden="true" />
              {t("title")}
            </DialogTitle>
            <DialogDescription>{t("description")}</DialogDescription>
          </div>
          <DialogClose asChild>
            <Button type="button" variant="ghost" size="icon-lg" className="-mt-1 -mr-1 shrink-0" aria-label={t("close")}>
              <X aria-hidden="true" />
            </Button>
          </DialogClose>
        </div>
        {/* A region of its own that takes focus, so a keyboard can scroll the list where the screen is short. */}
        <div
          role="region"
          aria-label={t("list")}
          tabIndex={0}
          className="grid min-h-0 flex-1 gap-5 overflow-y-auto px-4 py-4 focus-visible:outline-offset-[-2px] sm:px-5 md:grid-cols-2 md:gap-x-8"
          data-testid="shortcuts-list"
        >
          {COLUMNS.map((column) => (
            <div key={column.join("-")} className="flex min-w-0 flex-col gap-5">
              {column.map((group) => (
                <section key={group} aria-labelledby={`${ids}-${group}`} className="flex flex-col gap-1">
                  <h3 id={`${ids}-${group}`} className="text-xs font-semibold text-muted-foreground">
                    {t(`groups.${group}`)}
                  </h3>
                  <dl className="flex flex-col divide-y">
                    {SHORTCUTS.filter((entry) => entry.group === group).map((entry) => {
                      const off = entry.single && !characterKeys;
                      return (
                        <div
                          key={entry.id}
                          className="flex items-center justify-between gap-4 py-2"
                          data-testid="shortcut-row"
                          data-shortcut={entry.id}
                          data-off={off || undefined}
                        >
                          <dt className="flex min-w-0 flex-col text-[13px] leading-[18px]">
                            <span className="text-foreground">{t(`names.${entry.id}`)}</span>
                            {entry.where ? <span className="text-xs text-fg-subtle">{t(`where.${entry.where}`)}</span> : null}
                          </dt>
                          <dd className="flex shrink-0 items-center gap-2">
                            {off ? <span className="text-xs text-fg-subtle">{t("off")}</span> : null}
                            <KeySequence keys={entry.keys} modifier={modifier} />
                          </dd>
                        </div>
                      );
                    })}
                  </dl>
                </section>
              ))}
            </div>
          ))}
        </div>
        <div className="flex items-start gap-4 border-t bg-muted/50 px-4 py-3 sm:px-5">
          <div className="flex min-w-0 flex-1 flex-col gap-0.5">
            <label htmlFor={`${ids}-single`} className="cursor-pointer text-sm font-medium">
              {t("single.label")}
            </label>
            <p id={`${ids}-single-hint`} className="text-xs text-muted-foreground">
              {t("single.hint")}
            </p>
          </div>
          <Switch
            id={`${ids}-single`}
            checked={characterKeys}
            onCheckedChange={setCharacterKeys}
            aria-describedby={`${ids}-single-hint`}
            className="mt-0.5"
            data-testid="shortcuts-single"
          />
        </div>
      </DialogContent>
    </Dialog>
  );
}

/**
 * The second keys, shown at the foot of the screen when G has been pressed and no second key has come yet. For the
 * eye only: the dialog says the same in words, and a screen reader would read it after the second has passed.
 */
function LeadHint() {
  const t = useTranslations("shortcuts");
  const tNav = useTranslations("nav");
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none fixed bottom-6 left-1/2 z-50 flex -translate-x-1/2 items-center gap-3 rounded-md border bg-popover px-3 py-2 text-[13px] whitespace-nowrap text-muted-foreground shadow-popover max-md:hidden motion-safe:animate-in motion-safe:fade-in-0"
      data-testid="shortcuts-lead"
    >
      <span className="inline-flex items-center gap-1.5">
        <Kbd>G</Kbd>
        {t("then")}
      </span>
      {(Object.keys(GO) as GoKey[]).map((key) => (
        <span key={key} className="inline-flex items-center gap-1.5">
          <Kbd>{key.toUpperCase()}</Kbd>
          {tNav(GO[key].label)}
        </span>
      ))}
    </div>
  );
}
