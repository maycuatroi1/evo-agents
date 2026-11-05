"use client";

import { useTranslations } from "next-intl";
import dynamic from "next/dynamic";
import { usePathname, useRouter } from "next/navigation";
import { createContext, type ReactNode, useCallback, useContext, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { notify } from "@/components/feedback/toast";
import { useCanDispatch, useDispatchedToast } from "@/components/runs/hooks";
import { Kbd } from "@/components/ui/kbd";
import { useCharacterKeys, useModifierKey } from "@/lib/keyboard";
import { cn } from "@/lib/utils";

import { projectHref } from "./nav";
import { isPageDispatchRegistered, pageDispatch, registerPageDispatch, subscribePageDispatch } from "./page-dispatch";
import { useCurrentProject, useSelectedProject } from "./project-switcher";
import { BY_ID, strokeTexts, LEAD_MS, HINT_MS, GO, readKey, type ShortcutId, type GoKey } from "./shortcut-model";

export { LEAD_MS, NAV_SHORTCUTS, readKey, SHORTCUTS, type ShortcutId, shortcutText } from "./shortcut-model";

/**
 * The hub's keyboard shortcuts in one registry, and the shell's own keys. `SHORTCUTS` lists every key the web answers
 * to, for the shortcuts dialog (`?`) and for the `kbd` shown beside each action. Most keys are handled here: G then H,
 * I, P, R, C or W (within a second) go to Home, Inbox, the project's Plans, Runs and Curator, and Workers; D opens Dispatch on a
 * project's pages for a writer; ? opens the dialog. The others stay with what they act on: Cmd K or Ctrl K in
 * palette/palette-context.tsx, `/` in data/search-shortcut.ts, Cmd or Ctrl with Enter in the answer and message
 * forms, Cmd or Ctrl with B in ui/sidebar.tsx, J and K in inbox/inbox-page.tsx, Esc in each dialog.
 *
 * Single keys never take a key pressed with Cmd, Ctrl or Alt (the browser's and the screen reader's own), nor one
 * typed in a text field, the web terminal, a menu or a dialog (`characterKeyBlocked` in lib/keyboard.ts). The visitor
 * can turn them off in the dialog (WCAG 2.1.4); the shortcuts with Cmd or Ctrl stay.
 */

type ShortcutsState = {
  /** Opens the shortcuts dialog; focus goes back to `from` (by default what holds focus now) when it closes. */
  openShortcuts: (from?: HTMLElement | null) => void;
};

const ShortcutsContext = createContext<ShortcutsState | null>(null);

/** The shortcuts dialog's opener, or null outside the shell (a component rendered on its own in a test). */
export function useOpenShortcuts(): ShortcutsState["openShortcuts"] | null {
  return useContext(ShortcutsContext)?.openShortcuts ?? null;
}

/**
 * Lets D open the page's own Dispatch (its dialog, with the step of the page picked) while `open` is set. Returns
 * whether D does so now, for the page to show the key beside its button: in the shell, registered (never during the
 * server render and hydration, page-dispatch.ts), with single keys on.
 */
export function useDispatchShortcut(open: (() => void) | null): boolean {
  const id = useId();
  const inShell = useContext(ShortcutsContext) !== null;
  const enabled = open !== null && inShell;
  const registered = useSyncExternalStore(subscribePageDispatch, () => isPageDispatchRegistered(id), () => false);
  const characterKeys = useCharacterKeys();
  const latest = useRef(open);
  useEffect(() => {
    latest.current = open;
  });
  useEffect(() => {
    if (!enabled) return;
    return registerPageDispatch(id, () => latest.current?.());
  }, [enabled, id]);
  return enabled && registered && characterKeys;
}

const LazyDispatchDialog = dynamic(() => import("@/components/runs/dispatch-dialog").then((module) => module.DispatchDialog), {
  ssr: false,
});

// The shortcuts dialog's code is fetched when the dialog first opens, not with every page.
const LazyShortcutsDialog = dynamic(() => import("./shortcuts-dialog").then((module) => module.ShortcutsDialog), { ssr: false });

/**
 * The shell's keys and what they open: the shortcuts dialog, the hint after G, and Dispatch for a page that has no
 * Dispatch of its own (the dialog's code is fetched at the first D).
 */
export function ShortcutsProvider({ children }: { children: ReactNode }) {
  const t = useTranslations("shortcuts");
  const router = useRouter();
  const pathname = usePathname();
  const project = useCurrentProject();
  const selected = useSelectedProject();
  const canDispatch = useCanDispatch(project ?? "");
  const dispatched = useDispatchedToast();

  const [helpOpen, setHelpOpen] = useState(false);
  const [helpAsked, setHelpAsked] = useState(false);
  const [hint, setHint] = useState(false);
  const [dispatchIn, setDispatchIn] = useState<string | null>(null);
  const [dispatchOpen, setDispatchOpen] = useState(false);
  const opener = useRef<HTMLElement | null>(null);
  const lead = useRef<{ end: ReturnType<typeof setTimeout>; hint: ReturnType<typeof setTimeout> } | null>(null);

  // What a key press reads; kept current without adding the listener again, so a pending G survives a render.
  const now = useRef({ project, selected, canDispatch, pathname, router, t });
  useEffect(() => {
    now.current = { project, selected, canDispatch, pathname, router, t };
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
      setHelpAsked(true);
      setHelpOpen(true);
    },
    [remember],
  );

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
      const { selected: project, pathname: here, router, t } = now.current;
      if ("href" in target) {
        if (target.href !== here) router.push(target.href);
        return;
      }
      // Plans, Runs and the Curator are a project's: the one the sidebar shows (useSelectedProject).
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
        const page = pageDispatch();
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

  const value = useMemo(() => ({ openShortcuts }), [openShortcuts]);
  return (
    <ShortcutsContext.Provider value={value}>
      {children}
      {helpAsked ? <LazyShortcutsDialog open={helpOpen} onOpenChange={setHelpOpen} onCloseAutoFocus={giveFocusBack} /> : null}
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
