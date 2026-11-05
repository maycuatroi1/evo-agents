"use client";

import { Keyboard, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import { setCharacterKeys, useCharacterKeys, useModifierKey } from "@/lib/keyboard";

import { KeySequence } from "./key-sequence";
import { COLUMNS, SHORTCUTS } from "./shortcut-model";

/**
 * The shortcuts dialog: every key in the registry by group, each in a row with what it does, where it works when not
 * everywhere, and its keys; at the foot, the switch that turns single keys off on this browser.
 */
export function ShortcutsDialog({
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
