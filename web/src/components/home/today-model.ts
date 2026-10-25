import type { Today, TodayItem } from "@/lib/notification-queries";

/**
 * The pure parts of Home's Today (GET /v1/me/today): the window its items come from, and its summary sentence in a
 * language other than the hub's English, built from the same counts the hub's template counts.
 */

/** `today.ITEM_KINDS`, in the order the hub's summary names them. */
export const ITEM_KINDS = ["release", "step_done", "run_failed", "run_parked", "problem", "cost", "proposal"] as const satisfies readonly TodayItem["kind"][];
export type ItemKind = (typeof ITEM_KINDS)[number];

/**
 * seen: since the member last looked; day: no seen mark yet, the last 24 hours; week: a mark older than seven days,
 * the last seven. The hub starts the window at the mark itself unless it is older than that.
 */
export type TodayWindow = "seen" | "day" | "week";

export function windowOf(today: Pick<Today, "since" | "seen_at">): TodayWindow {
  if (today.seen_at === null) return "day";
  return Date.parse(today.since) > Date.parse(today.seen_at) + 1000 ? "week" : "seen";
}

/** How many items of each kind, in the hub's order, leaving out the kinds with none. */
export function countsByKind(items: readonly Pick<TodayItem, "kind">[]): { kind: ItemKind; count: number }[] {
  return ITEM_KINDS.map((kind) => ({ kind, count: items.filter((item) => item.kind === kind).length })).filter(({ count }) => count > 0);
}

/** The words a sentence is made of, from the page's messages. */
export type SummaryWords = {
  asks: (count: number) => string;
  nothing: (asks: string, window: TodayWindow) => string;
  part: (kind: ItemKind, count: number) => string;
  changed: (asks: string, window: TodayWindow, parts: string, more: boolean) => string;
  list: (parts: string[]) => string;
};

/**
 * The summary as the hub's template says it (`today.summary`): what waits for the member's answer, then what the
 * items are, by kind, and "and more" when they are not everything that changed. The hub's own sentence names what the
 * runs cost; this one, in another language, names the kind alone, and the cost item under it gives the figure.
 */
export function summaryOf(today: Pick<Today, "since" | "seen_at" | "needs_you_count" | "items" | "caught_up">, words: SummaryWords): string {
  const window = windowOf(today);
  const asks = words.asks(today.needs_you_count);
  const counts = countsByKind(today.items);
  if (counts.length === 0) return words.nothing(asks, window);
  return words.changed(asks, window, words.list(counts.map(({ kind, count }) => words.part(kind, count))), !today.caught_up);
}
