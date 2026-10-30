import { useTranslations } from "next-intl";
import type { ComponentPropsWithoutRef, ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * The elements of a tracked-changes document (lib/remark-tracked.ts) in SafeMarkdown's `tracked` mode. Added text is
 * `success` on `success-soft`; removed text is `danger` on `danger-soft` and struck through. Each mark also says what
 * it is in words for screen readers, so the colour is never the only cue. A whole block (a code block, a table) has
 * a bar on its start edge in the same colour. A `del` without `data-tracked` is the document's own strikethrough.
 */

type MarkProps = ComponentPropsWithoutRef<"del"> & { node?: unknown; "data-tracked"?: string; "data-block"?: string };

const INLINE = {
  add: "rounded-xs bg-success-soft px-0.5 text-success no-underline",
  del: "rounded-xs bg-danger-soft px-0.5 text-danger line-through decoration-danger",
} as const;

const BLOCK = {
  add: "my-4 block rounded-md border-s-4 border-success bg-success-soft/60 ps-3 py-1 no-underline [&_*]:text-success",
  del: "my-4 block rounded-md border-s-4 border-danger bg-danger-soft/60 ps-3 py-1 line-through decoration-danger [&_*]:text-danger",
} as const;

function Mark({ kind, block, children, className }: { kind: "add" | "del"; block: boolean; children: ReactNode; className?: string }) {
  const t = useTranslations("memories.markdown");
  const Tag = kind === "add" ? "ins" : "del";
  return (
    <Tag data-tracked={kind} data-block={block ? "" : undefined} className={cn(block ? BLOCK[kind] : INLINE[kind], className)}>
      <span className="sr-only">{kind === "add" ? t("trackedAdded") : t("trackedRemoved")} </span>
      {children}
    </Tag>
  );
}

export function TrackedIns({ node: _node, children, className, ...props }: MarkProps) {
  return (
    <Mark kind="add" block={props["data-block"] !== undefined} className={className}>
      {children}
    </Mark>
  );
}

export function TrackedDel({ node: _node, children, className, ...props }: MarkProps) {
  const kind = props["data-tracked"];
  if (kind !== "del") return <del className={className}>{children}</del>;
  return (
    <Mark kind="del" block={props["data-block"] !== undefined} className={className}>
      {children}
    </Mark>
  );
}
