import type { SrsComment } from "./api";

/**
 * The pure model of comments on a passage of an item (evo_agents.hub.server.srs.threads): the text an element shows,
 * flattened with white space as one space so a passage is found by its words whatever the markup, where a passage is,
 * and what a reader selected. The hub keeps the passage (`quote`), a little text around it (`prefix`, `suffix`), the
 * part of the item (`field`) and the item (`item`); the page finds the passage again in what it draws.
 */

export const CONTEXT_CHARS = 40; // of the text kept before and after a passage
export const MAX_QUOTE_CHARS = 2_000; // evo_agents.hub.tables_srs.MAX_QUOTE_CHARS

/** An element's text as one string, white space runs as one space, and where each of its characters comes from. */
export type FlatText = { text: string; at: { node: Text; offset: number }[] };

const BLOCKS = "p,li,td,th,dd,dt,h1,h2,h3,h4,h5,h6,div,tr,blockquote,pre,caption";

export function flatten(root: Node): FlatText {
  const at: FlatText["at"] = [];
  let text = "";
  let space = true; // drop leading white space, and every run after the first space
  let lastBlock: Element | null = null;
  let lastNode: Text | null = null;
  const walker = root.ownerDocument!.createTreeWalker(root, 4 /* NodeFilter.SHOW_TEXT */);
  for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
    const block = node.parentElement?.closest(BLOCKS) ?? null;
    if (lastNode && block !== lastBlock && !space) {
      // a block's end reads as a space between its words and the next block's
      text += " ";
      at.push({ node: lastNode, offset: lastNode.data.length });
      space = true;
    }
    const value = node.data;
    for (let index = 0; index < value.length; index += 1) {
      const blank = /\s/.test(value[index]);
      if (blank && space) continue;
      text += blank ? " " : value[index];
      at.push({ node, offset: index });
      space = blank;
    }
    lastBlock = block;
    lastNode = node;
  }
  return { text, at };
}

/** `text` with white space runs as one space, trimmed, case folded: how two passages are compared. */
export function words(text: string): string {
  return text.replace(/\s+/g, " ").trim().toLowerCase();
}

/** Where `quote` is in `flat` (start and end, end excluded), the occurrence after `prefix` first; null when absent. */
export function findQuote(flat: FlatText, quote: string, prefix?: string | null): { start: number; end: number } | null {
  const wanted = words(quote);
  if (!wanted) return null;
  const haystack = flat.text.toLowerCase();
  const hits: number[] = [];
  for (let from = haystack.indexOf(wanted); from >= 0; from = haystack.indexOf(wanted, from + 1)) hits.push(from);
  if (hits.length === 0) return null;
  const before = words(prefix ?? "");
  const after = before ? hits.find((start) => haystack.slice(Math.max(0, start - before.length - 1), start).includes(before)) : undefined;
  const best = after ?? hits[0];
  return { start: best, end: best + wanted.length };
}

/** A DOM Range over characters `start` to `end` of `flat`. */
export function rangeOf(flat: FlatText, start: number, end: number): Range | null {
  const first = flat.at[start];
  const last = flat.at[end - 1];
  if (!first || !last) return null;
  const range = first.node.ownerDocument!.createRange();
  range.setStart(first.node, first.offset);
  range.setEnd(last.node, Math.min(last.offset + 1, last.node.data.length));
  return range;
}

/** What a reader selected in an item: its text, the item and part it is in, and the text around it. */
export type Picked = { item: string; field: string; quote: string; prefix: string; suffix: string; offset: number };

/**
 * The passage `range` selects inside `container`, whose own item is `mainKey`: the item of the innermost element
 * marked `data-srs-item` around it (a business rule drawn inside its use case), the part of the closest one marked
 * `data-srs-field` or `data-field` (else the body), the selected text and up to CONTEXT_CHARS of text around it. Null
 * for a selection outside `container`, empty, or longer than MAX_QUOTE_CHARS.
 */
export function pickedIn(container: HTMLElement, range: Range, mainKey: string): Picked | null {
  if (!container.contains(range.commonAncestorContainer)) return null;
  const quote = words(range.toString()).length ? range.toString().replace(/\s+/g, " ").trim() : "";
  if (!quote || quote.length > MAX_QUOTE_CHARS) return null;
  const anchor = range.commonAncestorContainer instanceof Element ? range.commonAncestorContainer : range.commonAncestorContainer.parentElement;
  const itemEl = anchor?.closest<HTMLElement>("[data-srs-item]");
  const item = itemEl && container.contains(itemEl) ? (itemEl.dataset.srsItem ?? mainKey) : mainKey;
  const fieldEl = anchor?.closest<HTMLElement>("[data-srs-field],[data-field]");
  const field = (fieldEl && container.contains(fieldEl) ? (fieldEl.dataset.srsField ?? fieldEl.dataset.field) : null) ?? "body";
  const scope = itemEl && container.contains(itemEl) ? itemEl : container;
  const flat = flatten(scope);
  const found = findQuote(flat, quote);
  const start = found?.start ?? 0;
  const end = found?.end ?? 0;
  return {
    item,
    field,
    quote,
    prefix: found ? flat.text.slice(Math.max(0, start - CONTEXT_CHARS), start).trimStart() : "",
    suffix: found ? flat.text.slice(end, end + CONTEXT_CHARS).trimEnd() : "",
    offset: start,
  };
}

/** The element of `container` a passage of item `key` is looked for in: the item's own block, else `container`. */
export function scopeOf(container: HTMLElement, key: string | null): HTMLElement {
  if (!key) return container;
  const own = container.querySelector<HTMLElement>(`[data-srs-item="${CSS.escape(key)}"]`);
  return own ?? container;
}

/** The threads (roots, oldest first) on items of `keys`, each with its answers. */
export type PassageThread = { root: SrsComment; replies: SrsComment[] };

export function threadsOn(comments: readonly SrsComment[], keys: ReadonlySet<string>): PassageThread[] {
  const roots = comments.filter((one) => one.parent_id === null && one.item !== null && keys.has(one.item)).sort((a, b) => a.id - b.id);
  return roots.map((root) => ({ root, replies: comments.filter((one) => one.parent_id === root.id).sort((a, b) => a.id - b.id) }));
}
