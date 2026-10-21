"use client";

import { Info } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useId, useState } from "react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/**
 * An info icon beside a label whose text shows on hover, and on a click or a tap, which a tooltip alone does not
 * answer on a touch screen. It holds what a form says of a field beyond its label, so the form shows the choices only.
 * Focus alone does not show it, so a dialog that focuses its first control on opening shows no tooltip; a screen
 * reader reads the text as the button's description all the same, from a hidden copy that stays out of the name of a
 * legend or label around the icon. The button is named after `label`, the name of the field it tells about.
 */
export function InfoTip({ label, children, className, testId }: { label: string; children: ReactNode; className?: string; testId?: string }) {
  const t = useTranslations("infoTip");
  const id = useId();
  const [open, setOpen] = useState(false);
  return (
    <Tooltip open={open} onOpenChange={setOpen}>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={t("label", { field: label })}
          aria-describedby={`${id}-text`}
          className={cn(
            "relative inline-flex size-4 shrink-0 cursor-help items-center justify-center rounded-full text-fg-subtle transition-colors duration-150 outline-none after:absolute after:-inset-3 hover:text-foreground focus-visible:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50",
            className,
          )}
          // A prevented event skips the tooltip's own handler: focus does not open it, and a click toggles it.
          onFocus={(event) => event.preventDefault()}
          onClick={(event) => {
            event.preventDefault();
            setOpen((shown) => !shown);
          }}
          data-testid={testId}
        >
          <Info className="size-3.5" aria-hidden="true" />
        </button>
      </TooltipTrigger>
      <span id={`${id}-text`} hidden>
        {children}
      </span>
      <TooltipContent side="top" className="max-w-xs text-pretty" aria-hidden="true" data-testid={testId ? `${testId}-text` : undefined}>
        <span className="flex flex-col gap-1">{children}</span>
      </TooltipContent>
    </Tooltip>
  );
}
