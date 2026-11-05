/**
 * The codes a text names (an SRS item's `BR ORD1-1`, `UC-ORD-001`) as links to their pages. A code written as code
 * (`` `BR ORD1-1` ``) is linked when it is the whole span; text in a link or an image, and raw HTML, are left as they are.
 */

export type CodeLink = { code: string; href: string };

/** A text cut into its plain pieces and the codes it names, in order. */
export type SplitCodes = (text: string) => (string | CodeLink)[];

type MdNode = { type: string; value?: string; children?: MdNode[]; data?: Record<string, unknown> };

const LEFT = new Set(["code", "link", "linkReference", "image", "imageReference", "html", "definition"]);

function codeLink(href: string, children: MdNode[]): MdNode {
  return { type: "codeLink", data: { hName: "a", hProperties: { href, dataCode: "" } }, children };
}

/**
 * A remark plugin: each code `split` finds becomes an `a` with `data-code`, which SafeMarkdown renders as a link inside
 * the hub (`codes`). A link the Markdown writes never has that attribute: raw HTML never becomes an element there.
 */
export function remarkCodeLinks(split: SplitCodes) {
  const walk = (node: MdNode) => {
    if (!node.children || LEFT.has(node.type)) return;
    node.children = node.children.flatMap((child): MdNode[] => {
      if (child.type === "inlineCode") {
        const [whole, ...rest] = split(child.value ?? "");
        return whole && typeof whole !== "string" && rest.length === 0 ? [codeLink(whole.href, [child])] : [child];
      }
      if (child.type !== "text") {
        walk(child);
        return [child];
      }
      const pieces = split(child.value ?? "");
      if (pieces.length === 1 && typeof pieces[0] === "string") return [child];
      return pieces.map((piece) =>
        typeof piece === "string" ? { type: "text", value: piece } : codeLink(piece.href, [{ type: "text", value: piece.code }]),
      );
    });
  };
  return (tree: MdNode) => walk(tree);
}
