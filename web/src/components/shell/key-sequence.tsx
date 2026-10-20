"use client";

import { useTranslations } from "next-intl";
import { Fragment } from "react";

import { Kbd } from "@/components/ui/kbd";
import { type KeyPart, keyText, type ModifierKey } from "@/lib/keyboard";

/** A key in words, for screen readers: "Control", "Slash", "Question mark"; a letter as it is. */
function useSpokenKey() {
  const t = useTranslations("shortcuts.keys");
  return (part: KeyPart, modifier: ModifierKey): string => {
    if (part === "mod") return modifier === "meta" ? t("command") : t("control");
    if (part === "shift") return t("shift");
    if (part === "enter") return t("enter");
    if (part === "esc") return t("escape");
    if (part === "/") return t("slash");
    if (part === "?") return t("question");
    return part.toUpperCase();
  };
}

/** A shortcut's keys in the dialog: one `kbd` per key, "then" between the strokes of a sequence, and all of it in words. */
export function KeySequence({ keys, modifier }: { keys: readonly (readonly KeyPart[])[]; modifier: ModifierKey }) {
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
