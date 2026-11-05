"use client";

import { ChevronDown } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * The fold of a decision's context (`FoldedContext`, decision-view.tsx): `children` render `text`, clamped to four
 * lines with a button for the rest. Focus moving into the folded part unfolds it.
 */
export function ContextFold({ text, screen, children }: { text: string; screen: boolean; children: ReactNode }) {
  const t = useTranslations("inbox.decision");
  const ids = useId();
  const box = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [overflows, setOverflows] = useState(false);

  useEffect(() => {
    const element = box.current;
    if (!element || open) return;
    const measure = () => setOverflows(element.scrollHeight - element.clientHeight > 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [open, text]);

  return (
    <div className="flex flex-col items-start gap-1" data-testid="decision-context" data-folded={!open && overflows ? "true" : "false"}>
      <div
        id={`${ids}-context`}
        ref={box}
        role="group"
        aria-label={t("context")}
        className={cn("w-full max-w-prose", screen ? "text-[15px] leading-[22px]" : "text-[13px] leading-relaxed", !open && "line-clamp-4")}
        onFocus={() => {
          if (!open && overflows) setOpen(true);
        }}
      >
        {children}
      </div>
      {overflows || open ? (
        <Button
          type="button"
          variant="link"
          size="sm"
          className={cn("h-auto min-h-6 px-0", screen ? "text-sm max-md:min-h-11" : "text-xs")}
          aria-expanded={open}
          aria-controls={`${ids}-context`}
          onClick={() => setOpen(!open)}
          data-testid="decision-context-toggle"
        >
          <ChevronDown className={cn("transition-transform duration-base motion-reduce:transition-none", open && "rotate-180")} aria-hidden="true" />
          {open ? t("showLess") : t("showMore")}
        </Button>
      ) : null}
    </div>
  );
}
