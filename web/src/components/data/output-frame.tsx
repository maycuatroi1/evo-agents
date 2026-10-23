"use client";

import { Download, type LucideIcon } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { baseName, downloadHref, type ViewedFile } from "@/lib/outputs";

import { OutputKindIcon } from "./output-tile";

/** The frames of the viewer's bodies (output-views.tsx, output-text.tsx). */

export function Centered({ children }: { children: ReactNode }) {
  return <div className="flex min-h-0 flex-1 items-center justify-center p-3 sm:p-6">{children}</div>;
}

/** A file the viewer cannot show: its icon, why, and its download. */
export function NoPreview({ output, children, icon }: { output: ViewedFile; children: ReactNode; icon?: LucideIcon }) {
  const t = useTranslations("outputs.viewer");
  const Icon = icon;
  return (
    <Centered>
      <div className="flex max-w-sm flex-col items-center gap-3 text-center" data-testid="output-no-preview">
        <span className="flex size-10 items-center justify-center rounded-md bg-card text-muted-foreground">
          {Icon ? <Icon className="size-5" aria-hidden="true" /> : <OutputKindIcon output={output} className="size-5" />}
        </span>
        <p className="text-sm text-muted-foreground">{children}</p>
        <Button asChild variant="outline">
          <a href={downloadHref(output.url)} download={baseName(output.name)}>
            <Download aria-hidden="true" />
            {t("download")}
          </a>
        </Button>
      </div>
    </Centered>
  );
}
