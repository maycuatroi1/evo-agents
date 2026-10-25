import type { MarketPlugin } from "./queries";

/**
 * What the market pages decide without React: the filters a list keeps in its address, which plugins they leave, and what
 * a visitor may do with a plugin.
 */

export type MarketFilters = { q: string; industry: string | null; category: string | null };

export const NO_FILTERS: MarketFilters = { q: "", industry: null, category: null };

const MAX_QUERY = 200;
const MAX_FACET = 64;

function facet(value: string | null): string | null {
  const trimmed = (value ?? "").trim();
  return trimmed && trimmed.length <= MAX_FACET ? trimmed : null;
}

/** The filters of `?q=&industry=&category=`, trimmed; a facet longer than the hub allows is no filter. */
export function parseFilters(params: { get: (name: string) => string | null }): MarketFilters {
  return {
    q: (params.get("q") ?? "").trim().slice(0, MAX_QUERY),
    industry: facet(params.get("industry")),
    category: facet(params.get("category")),
  };
}

/** The address of the list with `filters`, the ones in force alone. */
export function filtersSearch(filters: MarketFilters): string {
  const params = new URLSearchParams();
  if (filters.q) params.set("q", filters.q);
  if (filters.industry) params.set("industry", filters.industry);
  if (filters.category) params.set("category", filters.category);
  const text = params.toString();
  return text ? `?${text}` : "";
}

export function activeFilters(filters: MarketFilters): number {
  return (filters.q ? 1 : 0) + (filters.industry ? 1 : 0) + (filters.category ? 1 : 0);
}

/** Whether `plugin` holds every word of `q` in its id, name, description or publisher, as GET /v1/market reads it. */
export function matchesQuery(plugin: MarketPlugin, q: string): boolean {
  const words = q.toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const haystack = [plugin.id, plugin.name, plugin.description, plugin.publisher.id, plugin.publisher.name ?? ""]
    .join(" ")
    .toLocaleLowerCase();
  return words.every((word) => haystack.includes(word));
}

export function filterPlugins(plugins: readonly MarketPlugin[], filters: MarketFilters): MarketPlugin[] {
  return plugins.filter(
    (plugin) =>
      (!filters.industry || plugin.industries.includes(filters.industry)) &&
      (!filters.category || plugin.categories.includes(filters.category)) &&
      (!filters.q || matchesQuery(plugin, filters.q)),
  );
}

/** What a hub admin may do with `plugin` from the market: install it, upgrade it, or nothing here. */
export function storeAction(plugin: Pick<MarketPlugin, "status" | "tier" | "price">): "install" | "upgrade" | null {
  if (plugin.tier !== "isolated" || plugin.price.kind !== "free") return null;
  if (plugin.status === "not_installed") return "install";
  if (plugin.status === "upgrade") return "upgrade";
  return null;
}

/** The projects a member may ask a hub admin to install a plugin for: those they administer, by name. */
export function requestableProjects(grants: readonly { project: string; role: string }[]): string[] {
  return [...new Set(grants.filter((grant) => grant.role === "admin").map((grant) => grant.project))].sort();
}

/** Whether `message`, a 409 of an install, says the hub does not trust the publisher's signer yet. */
export function isUntrusted(message: string | null | undefined): boolean {
  return /untrusted publisher/i.test(message ?? "");
}

/** OWNER/REPO as GitHub names a repo (evo_agents.plugins.market.REPO). */
export const REPO_PATTERN = /^[A-Za-z0-9_.-]{1,100}\/[A-Za-z0-9_.-]{1,100}$/;

/** The repo of an address someone pasted (https://github.com/OWNER/REPO, with .git or a path after it), or as typed. */
export function repoOf(text: string): string {
  const trimmed = text.trim();
  const found = /^(?:https?:\/\/)?github\.com\/([^/\s]+\/[^/\s#?]+)/i.exec(trimmed);
  return (found ? found[1] : trimmed).replace(/\.git$/, "");
}

/** The most months one order may cover (evo_agents.hub.tables_seller.MAX_MONTHS). */
export const MAX_MONTHS = 36;

/**
 * What a hub admin may do with a paid plugin: buy it while the hub has none, renew it once installed (a renewal starts
 * where the last licence ends), or nothing for a free or core one.
 */
export function buyAction(plugin: Pick<MarketPlugin, "status" | "tier" | "price">): "buy" | "renew" | null {
  if (plugin.tier !== "isolated" || plugin.price.kind === "free") return null;
  if (plugin.status === "needs_license") return "buy";
  if (plugin.status === "installed" || plugin.status === "upgrade") return "renew";
  return null;
}

/** The months typed into the Buy dialog, when they are a whole number the seller takes. */
export function parseMonths(text: string): number | null {
  if (!/^\d{1,2}$/.test(text.trim())) return null;
  const months = Number(text.trim());
  return months >= 1 && months <= MAX_MONTHS ? months : null;
}

/** What `months` months cost at `usdPerMonth`, to the cent, as the seller totals an order; null without a price. */
export function orderTotal(usdPerMonth: number | null | undefined, months: number | null): number | null {
  if (usdPerMonth === null || usdPerMonth === undefined || months === null) return null;
  return Math.round(usdPerMonth * 100 * months) / 100;
}

/** Whether `email` reads as an address the seller takes (one @, a dot in its domain). */
export function isEmail(email: string): boolean {
  return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim());
}

/** YYYY-MM of `now` in UTC, the month the seller's revenue reads by default. */
export function monthOf(now: Date): string {
  return `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, "0")}`;
}

/** The month before YYYY-MM `month`. */
export function previousMonth(month: string): string {
  const [year, number] = month.split("-").map(Number);
  return number === 1 ? `${year - 1}-12` : `${year}-${String(number - 1).padStart(2, "0")}`;
}

/** Whether YYYY-MM `month` is over at `now`, in UTC: a publisher's share is paid out only after it. */
export function monthOver(month: string, now: Date): boolean {
  return month < monthOf(now);
}

/** Whether a hub admin may mark this publisher's share of `month` paid out: once, after the month, when there is one. */
export function mayPayOut(line: { publisher_usd: number; paid_out?: unknown }, month: string, now: Date): boolean {
  return !line.paid_out && line.publisher_usd > 0 && monthOver(month, now);
}

export type LicenseTone = "success" | "attention" | "danger" | "neutral";

/** How a purchase's licence reads: active, in its grace week, lapsed, or still waiting for payment. */
export function licenseTone(state: string | null | undefined): LicenseTone {
  if (state === "active") return "success";
  if (state === "grace") return "attention";
  if (state === "lapsed" || state === "cancelled") return "danger";
  return "neutral";
}
