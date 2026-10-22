import {
  HANDLE,
  type Level,
  LOGIN,
  MAX_NAME_CHARS,
  MAX_NOTE_CHARS,
  type OrgSeat,
  type OrgUnit,
  type SeatAdd,
  type SeatEdit,
  type SeatRole,
  SLUG,
  type UnitCreate,
  type UnitEdit,
} from "./queries";

/**
 * The forms of the Org page's dialogs, and the bodies they send: a create sends what the form holds, an edit only the
 * fields that changed (the API keeps a field left out, and clears reports_to or ends_on sent as null). The checks are
 * those of the API's models, said at the field before the hub is asked; the rules of who may write stay the hub's.
 */

export type Problem = "slug" | "slugTaken" | "name" | "login" | "handle" | "project" | "reportsTo" | "internReportsTo" | "sponsor" | "agentSponsor" | "dates" | "note" | "level";
export type Problems<F extends string> = Partial<Record<F, Problem>>;

export type UnitForm = { slug: string; name: string; parent: string; kind: "team" | "cohort"; endsOn: string; level: Level | ""; note: string };
export type UnitField = "slug" | "name" | "note";

export function emptyUnitForm(parent: string): UnitForm {
  return { slug: "", name: "", parent, kind: "team", endsOn: "", level: "", note: "" };
}

export function unitFormOf(unit: OrgUnit): UnitForm {
  return { slug: unit.slug, name: unit.name, parent: unit.parent ?? "", kind: unit.kind === "cohort" ? "cohort" : "team", endsOn: unit.ends_on ?? "", level: "", note: "" };
}

export function unitProblems(form: UnitForm, { adding, taken }: { adding: boolean; taken: readonly string[] }): Problems<UnitField> {
  const found: Problems<UnitField> = {};
  if (adding && !SLUG.test(form.slug)) found.slug = "slug";
  else if (adding && taken.includes(form.slug)) found.slug = "slugTaken";
  if (form.name.length > MAX_NAME_CHARS || (!adding && form.name.trim() === "")) found.name = "name";
  if (form.note.length > MAX_NOTE_CHARS) found.note = "note";
  return found;
}

export function unitCreateBody(form: UnitForm): UnitCreate {
  return {
    slug: form.slug,
    parent: form.parent,
    kind: form.kind,
    ...(form.name.trim() ? { name: form.name.trim() } : {}),
    ...(form.kind === "cohort" && form.endsOn ? { ends_on: form.endsOn } : {}),
    ...(form.level ? { level: form.level } : {}),
    ...(form.note.trim() ? { note: form.note.trim() } : {}),
  };
}

/** The fields of `unit` the form changed, with the revision the edit was made against (409 when it moved). */
export function unitEditBody(unit: OrgUnit, form: UnitForm): UnitEdit {
  const body: UnitEdit = { if_revision: unit.revision };
  if (form.name.trim() !== unit.name) body.name = form.name.trim();
  if (unit.parent !== null && form.parent !== unit.parent) body.parent = form.parent;
  if (unit.kind === "cohort" && form.endsOn !== (unit.ends_on ?? "")) body.ends_on = form.endsOn || null;
  return body;
}

export type SeatForm = {
  who: "person" | "agent";
  login: string;
  agent: string;
  project: string;
  unit: string;
  role: SeatRole;
  reportsTo: string;
  sponsor: string;
  /** Asked to be the person's primary seat; unchecked leaves it to the hub (primary when theirs now is at the root). */
  primary: boolean;
  startsOn: string;
  endsOn: string;
};
export type SeatField = "login" | "agent" | "project" | "reportsTo" | "sponsor" | "endsOn";

export function emptySeatForm(unit: string, role: SeatRole, login = ""): SeatForm {
  return { who: "person", login, agent: "", project: "", unit, role, reportsTo: "", sponsor: "", primary: false, startsOn: "", endsOn: "" };
}

export function seatFormOf(seat: OrgSeat): SeatForm {
  return {
    who: seat.agent ? "agent" : "person",
    login: seat.login ?? "",
    agent: seat.agent ?? "",
    project: seat.project ?? "",
    unit: seat.unit,
    role: seat.role,
    reportsTo: seat.reports_to ?? "",
    sponsor: seat.sponsor ?? "",
    primary: seat.primary,
    startsOn: seat.starts_on,
    endsOn: seat.ends_on ?? "",
  };
}

export function seatProblems(form: SeatForm, adding: boolean): Problems<SeatField> {
  const found: Problems<SeatField> = {};
  const agent = form.who === "agent";
  if (adding && !agent && !LOGIN.test(form.login)) found.login = "login";
  if (adding && agent && !HANDLE.test(form.agent)) found.agent = "handle";
  if (adding && agent && !HANDLE.test(form.project)) found.project = "project";
  if (form.reportsTo && !LOGIN.test(form.reportsTo)) found.reportsTo = "reportsTo";
  else if (form.role === "intern" && !form.reportsTo) found.reportsTo = "internReportsTo";
  if (form.sponsor && !LOGIN.test(form.sponsor)) found.sponsor = "sponsor";
  else if (agent && !form.sponsor) found.sponsor = "agentSponsor";
  if (form.startsOn && form.endsOn && form.endsOn < form.startsOn) found.endsOn = "dates";
  return found;
}

export function seatAddBody(form: SeatForm): SeatAdd {
  const agent = form.who === "agent";
  return {
    unit: form.unit,
    role: agent ? "agent" : form.role,
    ...(agent ? { agent: form.agent, project: form.project } : { login: form.login }),
    ...(form.reportsTo ? { reports_to: form.reportsTo } : {}),
    ...(form.sponsor ? { sponsor: form.sponsor } : {}),
    ...(!agent && form.primary ? { primary: true } : {}),
    ...(form.startsOn ? { starts_on: form.startsOn } : {}),
    ...(form.endsOn ? { ends_on: form.endsOn } : {}),
  };
}

/** The fields of `seat` the form changed; an empty body changes nothing. */
export function seatEditBody(seat: OrgSeat, form: SeatForm): SeatEdit {
  const body: SeatEdit = {};
  if (form.role !== seat.role) body.role = form.role;
  if (form.reportsTo !== (seat.reports_to ?? "")) body.reports_to = form.reportsTo || null;
  if (form.sponsor && form.sponsor !== (seat.sponsor ?? "")) body.sponsor = form.sponsor;
  if (form.startsOn && form.startsOn !== seat.starts_on) body.starts_on = form.startsOn;
  if (form.endsOn !== (seat.ends_on ?? "")) body.ends_on = form.endsOn || null;
  if (form.primary && !seat.primary) body.primary = true;
  return body;
}
