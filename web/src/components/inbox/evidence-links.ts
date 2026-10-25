import { projectHref } from "@/components/shell/nav";
import { problemsHref } from "@/lib/problem-links";

import type { Decision } from "./queries";

/**
 * A ref of a decision's evidence (`decision_asks.evidence_ref`, as the hub returns it) as a link of the web: the run
 * and its page, the files of the decision's run, a problem's evidence on the problem's page, a memory, a file of a repo
 * through the project's knowledge graph search, or an https page, which opens in a new tab. A session or a transcript
 * line has no page of its own, so it is named, not linked. Only the form is read: the hub vouches for none of it.
 */
export type RefWords = "run" | "runEvent" | "output" | "problem" | "url" | "memory" | "code" | "codeLine" | "session" | "transcript" | "other";

export type EvidenceLink = {
  ref: string;
  /** The page it opens; null for a ref with no page. */
  href: string | null;
  /** An https page outside the hub, opened in a new tab. */
  external: boolean;
  /** An identifier the label shows in mono. */
  mono: boolean;
  words: RefWords;
  values: Record<string, string | number>;
};

const RUN = /^run:([1-9][0-9]{0,18})(?::([1-9][0-9]{0,9}))?$/;
const OUTPUT = /^output:([1-9][0-9]{0,18})$/;
const PROBLEM = /^(P-[1-9][0-9]{0,9})\/(E-[1-9][0-9]{0,9})$/;
const MEMORY = /^memory:([1-9][0-9]{0,18}):([1-9][0-9]{0,9})$/;
const CODE = /^code:([^:]+):(.+?)(?::([1-9][0-9]{0,9}))?$/;
const SESSION = /^session:([^:]+)/;
const TRANSCRIPT = /^transcript:([^:]+):(?:.+:)?([1-9][0-9]{0,9})$/;
const URL_WORDS = 64;

function link(ref: string, words: RefWords, values: Record<string, string | number>, href: string | null, extra: Partial<EvidenceLink> = {}): EvidenceLink {
  return { ref, href, external: false, mono: false, words, values, ...extra };
}

/** An https URL as a short label: its host and path, cut to a line. */
function urlWords(url: URL): string {
  const path = url.pathname === "/" ? "" : url.pathname;
  const text = `${url.host}${path}`;
  return text.length > URL_WORDS ? `${text.slice(0, URL_WORDS - 3)}...` : text;
}

export function evidenceLink(ref: string, decision: Pick<Decision, "project" | "run_id">): EvidenceLink {
  const { project } = decision;
  let found = RUN.exec(ref);
  if (found) {
    const href = projectHref(project, `runs/${found[1]}`);
    return found[2] ? link(ref, "runEvent", { id: found[1], seq: found[2] }, href) : link(ref, "run", { id: found[1] }, href);
  }
  if ((found = OUTPUT.exec(ref))) {
    return link(ref, "output", { id: found[1], run: decision.run_id }, `${projectHref(project, `runs/${decision.run_id}`)}?view=outputs`);
  }
  if ((found = PROBLEM.exec(ref))) {
    return link(ref, "problem", { problem: found[1], evidence: found[2] }, problemsHref(project, found[1]));
  }
  if ((found = MEMORY.exec(ref))) {
    return link(ref, "memory", { id: found[1], revision: found[2] }, projectHref(project, `memories/${found[1]}`));
  }
  if ((found = CODE.exec(ref))) {
    const [, repo, path, line] = found;
    const href = `${projectHref(project, "kg")}?q=${encodeURIComponent(path)}`;
    return line ? link(ref, "codeLine", { repo, path, line }, href, { mono: true }) : link(ref, "code", { repo, path }, href, { mono: true });
  }
  if ((found = TRANSCRIPT.exec(ref))) return link(ref, "transcript", { id: found[1].slice(0, 8), line: found[2] }, null, { mono: true });
  if ((found = SESSION.exec(ref))) return link(ref, "session", { id: found[1].slice(0, 8) }, null, { mono: true });
  if (ref.slice(0, 8).toLowerCase() === "https://") {
    try {
      const url = new URL(ref);
      if (url.protocol === "https:" && url.hostname) return link(ref, "url", { page: urlWords(url) }, url.href, { external: true });
    } catch {
      // not a URL after all: named below
    }
  }
  return link(ref, "other", { ref }, null, { mono: true });
}
