/**
 * The two pieces of raw HTML a Markdown document holds that SafeMarkdown does not show as their characters: a comment
 * (an importer's `<!-- source: ... -->`), a note for whoever edits the text, is dropped; a bare `<br>`, which a table
 * cell needs for a line break, becomes one. Any other HTML, a `<br>` with attributes too, stays text (no rehype-raw).
 */

type MdNode = { type: string; value?: string; children?: MdNode[] };

const COMMENTS = /^\s*(?:<!--[\s\S]*?-->\s*)+$/;
const BREAK = /^\s*<br\s*\/?>\s*$/i;

/** A remark plugin: HTML comments out of the tree, each bare `<br>` a `break`. */
export function remarkHtmlNotes() {
  const walk = (node: MdNode) => {
    if (!node.children) return;
    node.children = node.children.flatMap((child): MdNode[] => {
      if (child.type !== "html") {
        walk(child);
        return [child];
      }
      if (COMMENTS.test(child.value ?? "")) return [];
      return BREAK.test(child.value ?? "") ? [{ type: "break" }] : [child];
    });
  };
  return (tree: MdNode) => walk(tree);
}
