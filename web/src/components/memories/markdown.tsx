import { CheckSquare, ImageIcon, Square } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ComponentPropsWithoutRef, JSX, ReactNode } from "react";
import ReactMarkdown, { type Components, defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/utils";

/**
 * A memory body as Markdown (CommonMark and GitHub's tables, task lists, strikethrough and footnotes), never as
 * HTML: react-markdown turns raw HTML into text instead of markup (no rehype-raw here), so a <script> or an
 * onerror attribute in a memory shows as the characters it is. On top of that:
 *
 * - only the elements below are rendered; anything else is unwrapped to its text;
 * - links keep http(s) and mailto targets and in-page anchors (footnotes); any other target, `javascript:` or a
 *   path relative to the file, renders as text with the target in a title, since it means nothing on the web;
 * - images are not loaded (a memory must not make the browser fetch another site): they become a link to the
 *   image, or a placeholder;
 * - headings move down two levels (a memory's # sits under the page's h1 and the card's h2);
 * - code blocks and tables scroll inside a focusable region instead of widening the page.
 */
const ALLOWED = [
  "p",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "ul",
  "ol",
  "li",
  "blockquote",
  "pre",
  "code",
  "em",
  "strong",
  "del",
  "a",
  "hr",
  "br",
  "table",
  "thead",
  "tbody",
  "tr",
  "th",
  "td",
  "input",
  "img",
  "sup",
  "section",
];

const SAFE_LINK = /^(https?:|mailto:|#)/i;

function safeHref(href: string | undefined): string | null {
  if (!href) return null;
  const cleaned = defaultUrlTransform(href);
  return cleaned && SAFE_LINK.test(cleaned) ? cleaned : null;
}

type Props<T extends keyof JSX.IntrinsicElements> = ComponentPropsWithoutRef<T> & { node?: unknown };

function heading(level: 3 | 4 | 5 | 6, className: string) {
  const Tag = `h${level}` as const;
  return function Heading({ node: _node, className: extra, ...props }: Props<"h3">) {
    return <Tag className={cn("scroll-mt-20 font-semibold tracking-tight text-foreground", className, extra)} {...props} />;
  };
}

function MarkdownLink({ node: _node, href, children, className, ...props }: Props<"a">) {
  const t = useTranslations("memories.markdown");
  const target = safeHref(href);
  if (!target) {
    return (
      <span className="underline decoration-dotted underline-offset-4" title={t("inertLink", { target: href ?? "" })}>
        {children}
      </span>
    );
  }
  const external = /^https?:/i.test(target);
  return (
    <a
      {...props}
      href={target}
      className={cn("font-medium text-primary underline underline-offset-4 hover:no-underline", className)}
      {...(external ? { rel: "noopener noreferrer nofollow", target: "_blank" } : {})}
    >
      {children}
      {external ? <span className="sr-only"> {t("newTab")}</span> : null}
    </a>
  );
}

function MarkdownImage({ src, alt }: Props<"img">) {
  const t = useTranslations("memories.markdown");
  const label = alt ? t("image", { alt }) : t("imageNoAlt");
  const target = typeof src === "string" ? safeHref(src) : null;
  const content = (
    <>
      <ImageIcon className="size-3.5 shrink-0" aria-hidden="true" />
      {label}
    </>
  );
  const chip = "inline-flex items-center gap-1 rounded border border-dashed px-1.5 py-0.5 text-xs";
  return target && /^https?:/i.test(target) ? (
    <a href={target} rel="noopener noreferrer nofollow" target="_blank" className={cn(chip, "text-primary")}>
      {content}
      <span className="sr-only"> {t("newTab")}</span>
    </a>
  ) : (
    <span className={cn(chip, "text-muted-foreground")}>{content}</span>
  );
}

function TaskBox({ checked }: Props<"input">) {
  const t = useTranslations("memories.markdown");
  const Icon = checked ? CheckSquare : Square;
  return (
    <span className="mr-1.5 inline-flex align-[-2px]">
      <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
      <span className="sr-only">{checked ? t("taskDone") : t("taskOpen")}</span>
    </span>
  );
}

function Pre({ node: _node, className, ...props }: Props<"pre">) {
  const t = useTranslations("memories.markdown");
  return (
    <pre
      tabIndex={0}
      aria-label={t("codeBlock")}
      className={cn(
        "my-4 overflow-x-auto rounded-lg border bg-muted/50 p-3 font-mono text-xs leading-relaxed text-foreground",
        className,
      )}
      {...props}
    />
  );
}

function MarkdownTable({ node: _node, className, ...props }: Props<"table">) {
  const t = useTranslations("memories.markdown");
  return (
    <div role="region" aria-label={t("table")} tabIndex={0} className="my-4 overflow-x-auto rounded-lg border">
      {/* Words stay whole in a table: it scrolls in its region rather than breaking them. */}
      <table className={cn("w-full border-collapse text-sm [overflow-wrap:normal]", className)} {...props} />
    </div>
  );
}

const COMPONENTS: Components = {
  h1: heading(3, "mt-6 mb-3 text-lg first:mt-0"),
  h2: heading(4, "mt-6 mb-2 text-base first:mt-0"),
  h3: heading(5, "mt-5 mb-2 text-sm first:mt-0"),
  h4: heading(6, "mt-4 mb-2 text-sm first:mt-0"),
  h5: heading(6, "mt-4 mb-2 text-sm first:mt-0"),
  h6: heading(6, "mt-4 mb-2 text-sm text-muted-foreground first:mt-0"),
  p: ({ node: _node, ...props }) => <p className="my-3 leading-relaxed first:mt-0 last:mb-0" {...props} />,
  ul: ({ node: _node, className, ...props }) => (
    <ul
      className={cn(
        "my-3 list-disc space-y-1 pl-6 marker:text-muted-foreground",
        className?.includes("contains-task-list") && "list-none pl-1",
        className,
      )}
      {...props}
    />
  ),
  ol: ({ node: _node, className, ...props }) => (
    <ol className={cn("my-3 list-decimal space-y-1 pl-6 marker:text-muted-foreground", className)} {...props} />
  ),
  li: ({ node: _node, ...props }) => <li className="leading-relaxed" {...props} />,
  blockquote: ({ node: _node, ...props }) => (
    <blockquote className="my-4 border-l-2 border-primary/40 pl-4 text-muted-foreground" {...props} />
  ),
  pre: Pre,
  code: ({ node: _node, className, ...props }) => (
    <code
      className={cn(
        "rounded bg-muted px-1 py-0.5 font-mono text-[0.85em] [overflow-wrap:anywhere]",
        "[pre_&]:bg-transparent [pre_&]:p-0 [pre_&]:[overflow-wrap:normal]",
        className,
      )}
      {...props}
    />
  ),
  a: MarkdownLink,
  img: MarkdownImage,
  input: TaskBox,
  hr: () => <hr className="my-6 border-border" />,
  table: MarkdownTable,
  th: ({ node: _node, ...props }) => (
    <th className="border-b bg-muted/50 px-3 py-2 text-left text-xs font-medium text-muted-foreground" {...props} />
  ),
  td: ({ node: _node, ...props }) => <td className="border-b px-3 py-2 align-top" {...props} />,
  section: ({ node: _node, ...props }) => (
    <section className="mt-6 border-t pt-4 text-xs text-muted-foreground" {...props} />
  ),
};

export function SafeMarkdown({
  children,
  className,
  testId = "memory-markdown",
}: {
  children: string;
  className?: string;
  /** Other pages that render people's or agents' Markdown (a decision's context) name it for their own tests. */
  testId?: string;
}): ReactNode {
  return (
    <div className={cn("text-sm text-foreground [overflow-wrap:anywhere]", className)} data-testid={testId}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={COMPONENTS}
        allowedElements={ALLOWED}
        unwrapDisallowed
        urlTransform={(url) => defaultUrlTransform(url)}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
