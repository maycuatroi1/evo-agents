"use client";

import { Maximize, Minus, Plus } from "lucide-react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";

/** The zoom buttons in the corner of every graph canvas (./sigma-canvas.tsx): in, out, and the whole graph. */
export function ZoomControls({ disabled, onZoom }: { disabled: boolean; onZoom: (how: "in" | "out" | "fit") => void }) {
  const t = useTranslations("kg.explorer");
  return (
    <div className="absolute right-3 bottom-3 flex flex-col overflow-hidden rounded-sm border bg-card shadow-raised">
      {(
        [
          ["in", Plus, t("zoomIn")],
          ["out", Minus, t("zoomOut")],
          ["fit", Maximize, t("fit")],
        ] as const
      ).map(([how, Icon, text]) => (
        <Button
          key={how}
          type="button"
          variant="ghost"
          size="icon-sm"
          className="cursor-pointer rounded-none max-md:size-11 [&:not(:last-child)]:border-b"
          aria-label={text}
          title={text}
          onClick={() => onZoom(how)}
          disabled={disabled}
          data-testid={`kg-canvas-${how}`}
        >
          <Icon aria-hidden="true" />
        </Button>
      ))}
    </div>
  );
}
