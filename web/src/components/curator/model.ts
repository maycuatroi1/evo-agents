import type { Route } from "next";

import type { ParamSource } from "@/components/admin/data";
import { parseRunId, runHref } from "@/components/runs/queries";

import { type CuratorState, type CuratorStatus, type Lens, type Proposal, PROPOSALS_PAGE, type ProposalAction, type ProposalQuery, type ProposalState } from "./queries";
import { codeUrl, type EvidenceView, type RepoInfo } from "./evidence-model";
import { DEFER_DAYS, isLens, LINE, MAX_NOTE_CHARS, PROPOSAL_STATES, type Tier, TIERS } from "./vocab";

/**
 * The Curator pages' pure parts: where a project's night shift stands in a word, the proposal list's filters in the
 * URL, what an answer may carry, where each piece of evidence leads, and which revision of the charter the page shows.
 * The hub's limits and vocabularies are in vocab.ts, the charter as a form and back in charter-model.ts; both are
 * exported from here too, as the pages import them.
 */

export {
  AUTO_MERGE_TIERS,
  DEFER_DAYS,
  isLens,
  LENSES,
  LIMITS,
  MAX_NOTE_CHARS,
  MIN_HOLDOUT,
  MIN_SAVING,
  PROPOSAL_STATES,
  RUNTIMES,
  type Tier,
  TIERS,
} from "./vocab";
export {
  CHARTER_FIELDS,
  type CharterErrors,
  type CharterField,
  type CharterForm,
  type CharterProblem,
  charterBody,
  charterForm,
  ownTimeZone,
  type RoleForm,
  timeZones,
} from "./charter-model";

function isProposalState(value: string | null | undefined): value is ProposalState {
  return (PROPOSAL_STATES as readonly string[]).includes(value ?? "");
}

// Where the night shift stands

/** A project's night shift in a word, as the pill beside the Curator's title says it: `off` without a charter. */
export type CuratorLook = CuratorState | "off";

export function curatorLook(status: Pick<CuratorStatus, "charter" | "state">): CuratorLook {
  return status.charter === null || !status.state ? "off" : status.state;
}

/**
 * What the visitor may do on the Curator's pages: an admin of the project writes the charter and answers proposals;
 * an admin, or the owner of one of its schedules, pauses and resumes the night shift once it has a schedule.
 */
export type CuratorRights = { charter: boolean; answer: boolean; pause: boolean };

export function curatorRights(
  status: Pick<CuratorStatus, "schedules"> | null,
  viewer: { login: string; role: string | null } | null,
): CuratorRights {
  const admin = viewer?.role === "admin";
  const owner = viewer !== null && (status?.schedules ?? []).some((schedule) => schedule.owner === viewer.login);
  return { charter: admin, answer: admin, pause: (status?.schedules.length ?? 0) > 0 && (admin || owner) };
}

// The proposals' filters

/** The most pages the URL may name: 50 a page up to the API's largest offset. */
const MAX_PAGE = 2000;

export type ProposalFilters = {
  state: ProposalState | null;
  tier: Tier | null;
  lens: Lens | null;
  /** The review run whose proposals the list shows, from a night's row. */
  run: number | null;
  page: number;
};

export const NO_PROPOSAL_FILTERS: ProposalFilters = { state: null, tier: null, lens: null, run: null, page: 1 };

export function readProposalFilters(source: ParamSource): ProposalFilters {
  const state = source.get("state");
  const tier = Number(source.get("tier") ?? "");
  const lens = source.get("lens");
  const page = Number(source.get("page") ?? "1");
  const tierText = source.get("tier");
  return {
    state: isProposalState(state) ? state : null,
    tier: tierText !== null && tierText !== "" && (TIERS as readonly number[]).includes(tier) ? (tier as Tier) : null,
    lens: isLens(lens) ? lens : null,
    run: parseRunId(source.get("run")?.trim() ?? ""),
    page: Number.isInteger(page) && page >= 1 && page <= MAX_PAGE ? page : 1,
  };
}

/** The URL's query for a set of filters, in a stable order; empty when nothing is set. */
export function proposalSearch(filters: ProposalFilters): string {
  const params = new URLSearchParams();
  if (filters.state) params.set("state", filters.state);
  if (filters.tier !== null) params.set("tier", String(filters.tier));
  if (filters.lens) params.set("lens", filters.lens);
  if (filters.run !== null) params.set("run", String(filters.run));
  if (filters.page > 1) params.set("page", String(filters.page));
  const text = params.toString();
  return text ? `?${text}` : "";
}

export function isFiltered(filters: ProposalFilters): boolean {
  return filters.state !== null || filters.tier !== null || filters.lens !== null || filters.run !== null;
}

export function proposalListQuery(filters: ProposalFilters): ProposalQuery {
  return {
    state: filters.state,
    tier: filters.tier,
    lens: filters.lens,
    run: filters.run,
    limit: PROPOSALS_PAGE,
    offset: (filters.page - 1) * PROPOSALS_PAGE,
  };
}

// Answers

/** An open or deferred proposal is answered; an accepted, rejected or dropped one is not. */
export function isAnswerable(proposal: Pick<Proposal, "state">): boolean {
  return proposal.state === "open" || proposal.state === "deferred";
}

export type AnswerProblem = "noteLine" | "noteLong" | "days" | null;

/** Why an answer cannot be sent as it stands: a note of more than one line or over 2,000 characters, or days out of 1 to 90. */
export function answerProblem(action: ProposalAction, note: string, days: string): AnswerProblem {
  const words = note.trim();
  if (words && !LINE.test(words)) return "noteLine";
  if ([...words].length > MAX_NOTE_CHARS) return "noteLong";
  if (action === "defer") {
    const value = Number(days);
    if (!Number.isInteger(value) || value < DEFER_DAYS.min || value > DEFER_DAYS.max) return "days";
  }
  return null;
}

/** The body of an answer: the action, the note when there is one, and the days of a deferral. */
export function answerBody(action: ProposalAction, note: string, days: string) {
  const words = note.trim();
  return {
    action,
    ...(words ? { note: words } : {}),
    ...(action === "defer" ? { defer_days: Number(days) } : {}),
  };
}

// Evidence: where each piece leads; the forge's addresses and a digest's entry are in evidence-model.ts

export { codeUrl, digestEntry, type EvidenceView, type RepoInfo, webBase } from "./evidence-model";

function str(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function int(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

/**
 * Where a piece of evidence leads, as the hub resolved it: a run's event, a session's digest, a line of a session's
 * transcript, a line of code.
 */
export function evidenceView(raw: Record<string, unknown>, project: string, repos: readonly RepoInfo[]): EvidenceView {
  const kind = raw.kind;
  if (kind === "run") {
    const runId = int(raw.run_id);
    const seq = int(raw.seq);
    if (runId !== null && seq !== null) return { kind: "run", runId, seq, href: `${runHref(project, runId)}?view=log` as Route };
  }
  if (kind === "session") {
    const session = str(raw.session_id);
    if (session !== null) return { kind: "session", session, field: str(raw.field), index: int(raw.index) };
  }
  if (kind === "transcript") {
    const session = str(raw.session_id);
    const line = int(raw.line);
    if (session !== null && line !== null) return { kind: "transcript", session, file: str(raw.file) ?? "main", line };
  }
  if (kind === "code") {
    const repo = str(raw.repo);
    const path = str(raw.path);
    if (repo !== null && path !== null) {
      const info = repos.find((item) => item.name === repo);
      const commit = str(raw.commit);
      const line = int(raw.line);
      return { kind: "code", repo, path, line, commit, url: codeUrl(info?.origin, commit ?? info?.default_branch ?? null, path, line) };
    }
  }
  return { kind: "other", text: JSON.stringify(raw) };
}

// The charter's page

/** `?revision=N` names an older revision to read; `?edit=1` the form, for an admin (never over an older revision). */
export function readCharterView(source: ParamSource): { revision: number | null; edit: boolean } {
  const text = source.get("revision") ?? "";
  const revision = /^[1-9][0-9]{0,8}$/.test(text) ? Number(text) : null;
  return { revision, edit: source.get("edit") === "1" && revision === null };
}

// Nights

/** A night's outcome, as its row says it: work in flight, every run done, a run that failed, or nothing queued. */
export type NightOutcome = "active" | "failed" | "done" | "empty";

export function nightOutcome(night: { runs: number; done: number; failed: number; active: number }): NightOutcome {
  if (night.active > 0) return "active";
  if (night.failed > 0) return "failed";
  if (night.runs > 0) return "done";
  return "empty";
}

/** "12.3%" of what a night may cost, for its meter; 0 without a budget. */
export function budgetShare(cost: number, budget: number | null): number {
  if (budget === null || budget <= 0) return 0;
  return Math.min(1, Math.max(0, cost / budget));
}
