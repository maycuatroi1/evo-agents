/**
 * The members a chat message names, marked in its Markdown (web/DESIGN.md, Chats): an `@handle` of the chat's agent or
 * an `@login` of its person, as `chats.mention_names` reads a mention, and a person's login written bare. Each becomes
 * a mark that shows the member's name without the `@`; text in code, a link or an image is left as it is.
 */

export type MentionMark = {
  /** The handle or login, as the text names it. */
  name: string;
  /** What the mark shows: an agent's name, a person's login. */
  label: string;
  kind: "agent" | "person";
  /** Marked written bare too, not only after `@`. */
  bare: boolean;
};

export type MentionPiece = string | MentionMark;

const NAME_CHAR = String.raw`[\p{L}\p{N}_@./-]`;
const AT = String.raw`@([A-Za-z0-9][A-Za-z0-9-]{0,99})`;
const SPECIAL = /[.*+?^${}()|[\]\\]/g;
// A login this short is a word too ("an", "bao"): it is marked after `@` only.
const MIN_BARE = 4;

/** The marks of a chat's members: its agents by handle, its people by login (bare too, when long enough). */
export function chatMentionMarks(chat: {
  agents: readonly { handle: string; name: string }[];
  people: readonly { login: string }[];
}): MentionMark[] {
  return [
    ...chat.agents.map((agent): MentionMark => ({ name: agent.handle, label: agent.name || agent.handle, kind: "agent", bare: false })),
    ...chat.people.map((person): MentionMark => ({ name: person.login, label: person.login, kind: "person", bare: person.login.length >= MIN_BARE })),
  ];
}

/** The pattern of an `@name` and of each bare name of `marks`, the longest first, none inside a longer word or path. */
function pattern(marks: readonly MentionMark[]): RegExp {
  const bare = marks
    .filter((mark) => mark.bare)
    .map((mark) => mark.name.replace(SPECIAL, "\\$&"))
    .sort((a, b) => b.length - a.length);
  const words = bare.length ? [String.raw`(${bare.join("|")})(?![\p{L}\p{N}_/-]|\.[\p{L}\p{N}])`] : [];
  return new RegExp(`(?<!${NAME_CHAR})(?:${[AT, ...words].join("|")})`, "giu");
}

/** `text` cut into its plain pieces and the marks of the members it names, in order. */
export function splitMentions(text: string, marks: readonly MentionMark[]): MentionPiece[] {
  if (!marks.length || !text) return text ? [text] : [];
  const byName = new Map(marks.map((mark) => [mark.name.toLowerCase(), mark]));
  const pieces: MentionPiece[] = [];
  let last = 0;
  for (const found of text.matchAll(pattern(marks))) {
    const name = (found[1] ?? found[2]).toLowerCase();
    const mark = byName.get(name);
    if (!mark || (found[1] === undefined && !mark.bare)) continue;
    if (found.index > last) pieces.push(text.slice(last, found.index));
    pieces.push(mark);
    last = found.index + found[0].length;
  }
  if (last < text.length) pieces.push(text.slice(last));
  return pieces;
}

/** The marks of a chat as the list of chats knows it: handles and logins alone, each shown as written. */
export function summaryMentionMarks(chat: { agents: readonly string[]; people: readonly string[] }): MentionMark[] {
  return [
    ...chat.agents.map((handle): MentionMark => ({ name: handle, label: handle, kind: "agent", bare: false })),
    ...chat.people.map((login): MentionMark => ({ name: login, label: login, kind: "person", bare: false })),
  ];
}

/** `text` with each member it mentions as its label, without the `@`: a preview of a message in one line. */
export function plainMentions(text: string, marks: readonly MentionMark[]): string {
  return splitMentions(text, marks)
    .map((piece) => (typeof piece === "string" ? piece : piece.label))
    .join("");
}

type MdNode = { type: string; value?: string; children?: MdNode[]; data?: Record<string, unknown> };

// Where a name is not a mention: code, a link's or an image's text, raw HTML (shown as text).
const LEFT = new Set(["code", "inlineCode", "link", "linkReference", "image", "imageReference", "html", "definition"]);

/**
 * A remark plugin: each member `marks` names in the text becomes a `span` with `data-mention` (its name) and
 * `data-kind`, holding its label. SafeMarkdown renders that span (`mentions`); no other span reaches it.
 */
export function remarkMentions(marks: readonly MentionMark[]) {
  const walk = (node: MdNode) => {
    if (!node.children || LEFT.has(node.type)) return;
    node.children = node.children.flatMap((child): MdNode[] => {
      if (child.type !== "text") {
        walk(child);
        return [child];
      }
      const pieces = splitMentions(child.value ?? "", marks);
      if (pieces.length === 1 && typeof pieces[0] === "string") return [child];
      return pieces.map((piece) =>
        typeof piece === "string"
          ? { type: "text", value: piece }
          : {
              type: "mention",
              data: {
                hName: "span",
                hProperties: { dataMention: piece.name.toLowerCase(), dataKind: piece.kind },
                hChildren: [{ type: "text", value: piece.label }],
              },
            },
      );
    });
  };
  return (tree: MdNode) => walk(tree);
}
