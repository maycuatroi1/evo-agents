/**
 * The emoji the reaction picker offers, each with the key of its name under `chats.emoji` (web/DESIGN.md, Style: the
 * one place the hub shows emoji). The first three are the ones an agent's run puts by rule (`chats.STATUS_REACTIONS`):
 * eyes when a worker claims the run, a check mark when it is done, a cross when it failed or was cancelled.
 */
export const PICKER = [
  { emoji: "\u{1F440}", name: "eyes" },
  { emoji: "✅", name: "check" },
  { emoji: "❌", name: "cross" },
  { emoji: "\u{1F44D}", name: "thumbsUp" },
  { emoji: "\u{1F44E}", name: "thumbsDown" },
  { emoji: "❤️", name: "heart" },
  { emoji: "\u{1F389}", name: "party" },
  { emoji: "\u{1F604}", name: "smile" },
  { emoji: "\u{1F615}", name: "confused" },
  { emoji: "\u{1F914}", name: "thinking" },
  { emoji: "\u{1F64F}", name: "thanks" },
  { emoji: "\u{1F680}", name: "rocket" },
  { emoji: "\u{1F525}", name: "fire" },
  { emoji: "\u{1F41B}", name: "bug" },
  { emoji: "⏳", name: "hourglass" },
  { emoji: "\u{1F4AF}", name: "hundred" },
] as const;

export type EmojiName = (typeof PICKER)[number]["name"];

const NAMES = new Map<string, EmojiName>(PICKER.map((item) => [item.emoji, item.name]));
// The same emoji written without its variation selector, as some keyboards send it.
NAMES.set("❤", "heart");

/** The key of an emoji's name, when the picker offers it; null for one put some other way (the CLI, an agent). */
export function emojiName(emoji: string): EmojiName | null {
  return NAMES.get(emoji) ?? null;
}
