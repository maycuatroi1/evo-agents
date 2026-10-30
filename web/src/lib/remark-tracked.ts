/**
 * The marks of a tracked-changes document (marked-markdown.ts) and the remark plugin that turns them into elements.
 *
 * The marks are six punctuation characters of the Supplemental Punctuation block, which no document is expected to hold
 * and which marked-markdown.ts replaces in both versions before it marks anything, so a document cannot forge one.
 * Being punctuation (Pi, Pf, Po), they leave CommonMark's emphasis rules as they were around them.
 *
 * - ADD_OPEN text ADD_CLOSE is text the new version added, DEL_OPEN text DEL_CLOSE text it removed;
 * - a paragraph holding BLOCK_ADD or BLOCK_DEL alone marks the block right after it (a code block, a table) whole.
 *
 * The plugin turns them into `ins` and `del` elements with `data-tracked` ("add" or "del", and `data-block` for a
 * whole block), which SafeMarkdown renders only in its `tracked` mode. Raw HTML in the document never becomes an
 * element (SafeMarkdown has no rehype-raw), so no `ins` and no `data-tracked` comes from anywhere else.
 */

export const ADD_OPEN = "\u2E02";
export const ADD_CLOSE = "\u2E03";
export const DEL_OPEN = "\u2E04";
export const DEL_CLOSE = "\u2E05";
export const BLOCK_DEL = "\u2E00";
export const BLOCK_ADD = "\u2E01";

/** Any of the marks, to replace them in a document before marking it. */
export const ANY_MARK = /[\u2E00-\u2E05]/g;

export type TrackedKind = "add" | "del";

type MdNode = {
  type: string;
  value?: string;
  url?: string;
  title?: string | null;
  alt?: string | null;
  children?: MdNode[];
  data?: Record<string, unknown>;
};

const SPLIT = /([\u2E02-\u2E05])/;
const HAS_MARK = /[\u2E00-\u2E05]/;

function strip(text: string): string {
  return text.replace(ANY_MARK, "");
}

function wrapper(kind: TrackedKind, children: MdNode[], block = false): MdNode {
  return {
    type: "trackedChange",
    data: {
      hName: kind === "add" ? "ins" : "del",
      hProperties: block ? { dataTracked: kind, dataBlock: "" } : { dataTracked: kind },
    },
    children,
  };
}

/** The text of a node that holds marks, kept to the version `keep` names. */
function version(text: string, keep: TrackedKind): string {
  let mode: TrackedKind | null = null;
  let out = "";
  for (const piece of text.split(SPLIT)) {
    if (piece === ADD_OPEN) mode = "add";
    else if (piece === DEL_OPEN) mode = "del";
    else if (piece === ADD_CLOSE || piece === DEL_CLOSE) mode = null;
    else if (mode === null || mode === keep) out += piece;
  }
  return out;
}

/** Pieces of a text with marks as [mode, text], the mode carried in and the one left at its end. */
function pieces(text: string, mode: TrackedKind | null): { parts: [TrackedKind | null, string][]; mode: TrackedKind | null } {
  const parts: [TrackedKind | null, string][] = [];
  for (const piece of text.split(SPLIT)) {
    if (piece === ADD_OPEN) mode = "add";
    else if (piece === DEL_OPEN) mode = "del";
    else if (piece === ADD_CLOSE || piece === DEL_CLOSE) mode = null;
    else if (piece) parts.push([mode, piece]);
  }
  return { parts, mode };
}

/**
 * The children of a phrasing node with each run of marked leaves wrapped in its element. A mark may open in one
 * node and close in a sibling or inside a child (a whole line wrapped around a strong word): the mode carries across,
 * and a container (strong, link) stays unwrapped while the leaves inside it are wrapped.
 */
function phrasing(parent: MdNode, carried: TrackedKind | null): TrackedKind | null {
  let mode = carried;
  const out: MdNode[] = [];
  const push = (node: MdNode, kind: TrackedKind | null) => {
    if (kind === null) {
      out.push(node);
      return;
    }
    const last = out[out.length - 1];
    if (last?.type === "trackedChange" && (last.data?.hProperties as { dataTracked?: string }).dataTracked === kind) last.children?.push(node);
    else out.push(wrapper(kind, [node]));
  };
  for (const child of parent.children ?? []) {
    if (child.type === "text" || child.type === "html" || child.type === "inlineCode") {
      // Raw HTML shows as text in SafeMarkdown, so it is cut like text.
      const type = child.type === "inlineCode" ? "inlineCode" : child.type;
      const found = pieces(child.value ?? "", mode);
      for (const [kind, text] of found.parts) push({ type, value: text }, kind);
      mode = found.mode;
      continue;
    }
    if (typeof child.url === "string") child.url = strip(child.url);
    if (typeof child.title === "string") child.title = strip(child.title);
    if (typeof child.alt === "string") child.alt = strip(child.alt);
    if (child.children) {
      mode = phrasing(child, mode);
      out.push(child);
    } else push(child, mode);
  }
  parent.children = out;
  return mode;
}

const PHRASING_ROOTS = new Set(["paragraph", "heading", "tableCell"]);

function markerOf(node: MdNode): TrackedKind | null {
  if (node.type !== "paragraph" || node.children?.length !== 1) return null;
  const only = node.children[0];
  if (only.type !== "text") return null;
  const value = (only.value ?? "").trim();
  return value === BLOCK_ADD ? "add" : value === BLOCK_DEL ? "del" : null;
}

function blocks(parent: MdNode) {
  const out: MdNode[] = [];
  let pending: TrackedKind | null = null;
  for (const child of parent.children ?? []) {
    const marker = markerOf(child);
    if (marker) {
      pending = marker;
      continue;
    }
    let node = child;
    if (node.type === "code" && HAS_MARK.test(node.value ?? "")) {
      // A code block with a changed line inside (one marked-markdown.ts did not see as a block): both versions whole.
      const value = node.value ?? "";
      const old: MdNode = { ...node, value: version(value, "del") };
      const now: MdNode = { ...node, value: version(value, "add") };
      out.push(wrapper("del", [old], true), wrapper("add", [now], true));
      pending = null;
      continue;
    }
    if (node.type === "html" && HAS_MARK.test(node.value ?? "")) node = { type: "paragraph", children: [{ type: "text", value: node.value }] };
    if (PHRASING_ROOTS.has(node.type)) phrasing(node, null);
    else if (node.children) blocks(node);
    if (typeof node.url === "string") node.url = strip(node.url);
    if (typeof node.title === "string") node.title = strip(node.title);
    out.push(pending ? wrapper(pending, [node], true) : node);
    pending = null;
  }
  parent.children = out;
}

/** The remark plugin of SafeMarkdown's `tracked` mode. */
export function remarkTracked() {
  return (tree: MdNode) => blocks(tree);
}
