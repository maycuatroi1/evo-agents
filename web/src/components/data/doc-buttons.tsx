"use client";

import { BookOpen } from "lucide-react";
import { useTranslations } from "next-intl";
import { createContext, useContext } from "react";

import { Button } from "@/components/ui/button";
import { docKey, isDoc } from "@/lib/doc-versions";
import { baseName, type ViewedFile } from "@/lib/outputs";
import { cn } from "@/lib/utils";

/** Opens a document beside the conversation by its name; null where the page has no panel (a message drawn elsewhere). */
export const OpenDocContext = createContext<((name: string) => void) | null>(null);

/**
 * A button for each text document among a message's files (`isDoc`: Markdown, plain text, JSON, CSV, YAML, code; not
 * an image, a PDF or an archive) that opens it in the document panel, once per name, where the page has the panel.
 */
export function DocButtons({ files, className, testId = "message-docs" }: { files: readonly ViewedFile[]; className?: string; testId?: string }) {
  const t = useTranslations("chats.doc");
  const openDoc = useContext(OpenDocContext);
  const byKey = new Map<string, string>();
  for (const file of files) if (isDoc(file) && !byKey.has(docKey(file.name))) byKey.set(docKey(file.name), baseName(file.name));
  const names = [...byKey.values()];
  if (!openDoc || names.length === 0) return null;
  return (
    <div className={cn("flex flex-wrap gap-1.5", className)} data-testid={testId}>
      {names.map((name) => (
        <Button key={name} type="button" variant="outline" size="sm" className="h-7 max-w-full text-xs" onClick={() => openDoc(name)} data-testid="doc-open">
          <BookOpen aria-hidden="true" />
          <span className="truncate">{t("open", { name })}</span>
        </Button>
      ))}
    </div>
  );
}
