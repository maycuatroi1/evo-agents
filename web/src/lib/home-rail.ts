import type { components } from "@/lib/api/schema";

type Summary = components["schemas"]["ChatSummary"];
type Brief = components["schemas"]["WorkBrief"];

/**
 * The rail of Home (web/DESIGN.md, Chat home): the tasks (work chats) and the group chats of the agent chosen, read from
 * the visitor's chat list (GET /v1/chats) in the browser, and the groups the tasks fall in.
 */

/** A failed task stays in Needs you this long after its last message. */
export const FAILED_NEEDS_YOU_MS = 7 * 86_400_000;

/** The tasks Earlier shows before the visitor asks for the older ones. */
export const EARLIER_SHOWN = 8;

/** The group chats the rail lists, the latest active first. */
export const GROUPS_SHOWN = 6;

export type RailSection = "needsYou" | "running" | "thisWeek" | "earlier";
export const RAIL_SECTIONS: readonly RailSection[] = ["needsYou", "running", "thisWeek", "earlier"];

export type HomeRail = Record<RailSection, Summary[]> & { groups: Summary[] };

/** When a chat was last active: its last message, else when it was made. */
export function lastActive(chat: Pick<Summary, "last_message_at" | "created_at">): number {
  return Date.parse(chat.last_message_at ?? chat.created_at);
}

/**
 * Whether a task waits on a person (plan agent-workspace-home, a5): its work needs a person, it failed in the last 7
 * days, or it is done with an outcome of not met that no member marked handled (the hub reads such an outcome as null
 * once one did).
 */
export function needsYou(work: Pick<Brief, "status" | "outcome">, activeAt: number, now: number): boolean {
  if (work.status === "needs_person") return true;
  if (work.status === "failed") return now - activeAt <= FAILED_NEEDS_YOU_MS;
  return work.status === "done" && work.outcome === "not_met";
}

/** The calendar day of `at` in `timeZone`, as midnight UTC of that date, and its weekday, Monday 0. */
function dayIn(at: number, timeZone: string): { utc: number; weekday: number } {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(at));
  const part = (type: string) => parts.find((item) => item.type === type)?.value ?? "";
  const utc = Date.parse(`${part("year")}-${part("month")}-${part("day")}T00:00:00Z`);
  return { utc, weekday: (new Date(utc).getUTCDay() + 6) % 7 };
}

/** Whether `at` falls in the week of `now` (Monday to Sunday) by the calendar of `timeZone`, the viewer's. */
export function inThisWeek(at: number, now: number, timeZone: string): boolean {
  const today = dayIn(now, timeZone);
  const weekStart = today.utc - today.weekday * 86_400_000;
  const day = dayIn(at, timeZone).utc;
  return day >= weekStart && day <= today.utc;
}

/** The section of a task: Needs you first, then Running, then by when it was last active. */
export function sectionOf(chat: Summary & { work: Brief }, now: number, timeZone: string): RailSection {
  const at = lastActive(chat);
  if (needsYou(chat.work, at, now)) return "needsYou";
  if (chat.work.status === "running") return "running";
  return inThisWeek(at, now, timeZone) ? "thisWeek" : "earlier";
}

/**
 * A title as the rail shows it: without the @mentions it starts with, the link it ends with, and the "X + @y:" that a
 * group chat named after its first message starts with. The title as it is when nothing would be left.
 */
export function railTitle(title: string): string {
  let text = title.trim();
  text = text.replace(/^[^:\n]{1,80}? \+ @[\w.-]+(?:,? @[\w.-]+)*:\s*/u, "");
  text = text.replace(/^(?:@[\w.-]+[\s,:]*)+/u, "");
  text = text.replace(/(?:\s*\(?<?https?:\/\/\S+)+\s*$/u, "");
  text = text.trim();
  return text || title.trim();
}

/** Whether the rail's search finds a chat: the words in its title, shown or raw, ignoring case. */
export function railMatches(chat: Pick<Summary, "title"> & { work?: Pick<Brief, "title"> | null }, query: string): boolean {
  const wanted = query.trim().toLowerCase();
  if (!wanted) return true;
  const title = chat.work?.title ?? chat.title;
  return title.toLowerCase().includes(wanted) || railTitle(title).toLowerCase().includes(wanted);
}

/**
 * The rail of agent `handle`: its tasks (work chats it is in) in their sections, each in the list's order (the latest
 * active first), and the group and settle chats it is in. A direct chat is the rail's Direct chat, not a row.
 */
export function homeRail(chats: readonly Summary[], handle: string, now: number, timeZone: string, query = ""): HomeRail {
  const rail: HomeRail = {
    needsYou: [],
    running: [],
    thisWeek: [],
    earlier: [],
    groups: [],
  };
  for (const chat of chats) {
    if (!chat.agents.includes(handle) || !railMatches(chat, query)) continue;
    if (chat.kind === "work" && chat.work) rail[sectionOf({ ...chat, work: chat.work }, now, timeZone)].push(chat);
    else if (chat.kind === "group" || chat.kind === "settle") rail.groups.push(chat);
  }
  return rail;
}
