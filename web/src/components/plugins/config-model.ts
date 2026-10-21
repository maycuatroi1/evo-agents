/**
 * The form of one stored layer of a plugin's config (the hub's own, or a project's), drawn from the JSON Schema the
 * plugin's manifest gives (docs/plugins.md, Config): no React, so it is tested on its own.
 *
 * Each field of the schema is one input by its kind: text, a number, a choice (an enum, or a boolean as On and Off),
 * a list of strings (one a line), JSON for anything else, and a secret. A layer sets some fields and leaves the rest
 * to the layers below it, so every input can be empty, "not set here"; emptying one unsets it. A secret is never shown
 * again: the form says whether this layer sets it and offers to replace or clear it, and its value lives only in the
 * input until it is sent. The checks repeat the ones the hub makes of one layer (type, bounds, length, pattern,
 * choices); what a field requires is the effective config's business, which the hub reports as its problems.
 */

export type FieldKind = "text" | "integer" | "number" | "choice" | "boolean" | "list" | "json" | "secret";

export type ConfigField = {
  name: string;
  title: string;
  description: string;
  kind: FieldKind;
  required: boolean;
  /** The schema's default, as text: what the field is when no layer sets it. */
  fallback: string | null;
  choices: unknown[];
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  pattern?: string;
};

/** A secret in the form: kept as the layer holds it, given anew (`value`), or cleared from the layer. */
export type SecretDraft = { mode: "keep" | "set" | "clear"; value: string };
export type Draft = Record<string, string | SecretDraft>;
export type Problem =
  | { kind: "integer" | "number" | "json" | "choice" | "pattern" | "secretEmpty" }
  | { kind: "minimum" | "maximum" | "minLength" | "maxLength"; limit: number };

type Schema = { properties?: Record<string, Record<string, unknown>>; required?: string[] } | null | undefined;
type Layer = { values: Record<string, unknown>; secrets: string[]; version: number };

function asNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function kindOf(spec: Record<string, unknown>): FieldKind {
  if (spec["x-secret"] === true) return "secret";
  if (Array.isArray(spec.enum)) return "choice";
  const type = Array.isArray(spec.type) ? spec.type.find((item) => item !== "null") : spec.type;
  if (type === "string") return "text";
  if (type === "integer") return "integer";
  if (type === "number") return "number";
  if (type === "boolean") return "boolean";
  const items = spec.items as Record<string, unknown> | undefined;
  if (type === "array" && items?.type === "string" && !Array.isArray(items.enum)) return "list";
  return "json";
}

/** The text an input holds for `value` of a field of kind `kind`. */
export function textOf(kind: FieldKind, value: unknown): string {
  if (value === undefined) return "";
  if (kind === "text") return typeof value === "string" ? value : JSON.stringify(value);
  if (kind === "integer" || kind === "number") return String(value);
  if (kind === "boolean" || kind === "choice") return JSON.stringify(value);
  if (kind === "list") return Array.isArray(value) ? value.map(String).join("\n") : JSON.stringify(value);
  return JSON.stringify(value, null, 2);
}

/**
 * The fields of a config schema: the required ones first, the secrets last, by title within each. The hub keeps a
 * manifest as JSONB, which does not keep the order its author wrote the properties in, so the form sets its own.
 */
export function fieldsOf(schema: Schema): ConfigField[] {
  const required = new Set(schema?.required ?? []);
  const rank = (field: ConfigField) => (field.required ? 0 : 1) + (field.kind === "secret" ? 2 : 0);
  return Object.entries(schema?.properties ?? {})
    .map(([name, spec]) => fieldOf(name, spec, required.has(name)))
    .sort((a, b) => rank(a) - rank(b) || a.title.localeCompare(b.title) || a.name.localeCompare(b.name));
}

function fieldOf(name: string, spec: Record<string, unknown>, required: boolean): ConfigField {
  const kind = kindOf(spec);
  const choices = kind === "boolean" ? [true, false] : Array.isArray(spec.enum) ? spec.enum : [];
  return {
    name,
    title: typeof spec.title === "string" && spec.title.trim() ? spec.title : name,
    description: typeof spec.description === "string" ? spec.description : "",
    kind,
    required,
    fallback: "default" in spec && kind !== "secret" ? textOf(kind, spec.default) : null,
    choices,
    minimum: asNumber(spec.minimum),
    maximum: asNumber(spec.maximum),
    minLength: asNumber(spec.minLength),
    maxLength: asNumber(spec.maxLength),
    pattern: typeof spec.pattern === "string" ? spec.pattern : undefined,
  };
}

/** The form of `layer` as it stands: each plain field as text, each secret kept. */
export function draftOf(fields: ConfigField[], layer: Layer): Draft {
  return Object.fromEntries(
    fields.map((field) => [
      field.name,
      field.kind === "secret" ? { mode: "keep", value: "" } : textOf(field.kind, layer.values[field.name]),
    ]),
  );
}

/** The value the text of a plain field stands for; throws a Problem when it stands for none. */
function parse(field: ConfigField, text: string): unknown {
  switch (field.kind) {
    case "text":
      return text;
    case "integer":
    case "number": {
      const value = Number(text.trim());
      if (text.trim() === "" || !Number.isFinite(value)) throw { kind: field.kind } satisfies Problem;
      if (field.kind === "integer" && !Number.isInteger(value)) throw { kind: "integer" } satisfies Problem;
      return value;
    }
    case "boolean":
    case "choice": {
      const value = field.choices.find((choice) => JSON.stringify(choice) === text);
      if (value === undefined) throw { kind: "choice" } satisfies Problem;
      return value;
    }
    case "list":
      return text
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
    default:
      try {
        return JSON.parse(text) as unknown;
      } catch {
        throw { kind: "json" } satisfies Problem;
      }
  }
}

function bounds(field: ConfigField, value: unknown): Problem | null {
  if (typeof value === "number") {
    if (field.minimum !== undefined && value < field.minimum) return { kind: "minimum", limit: field.minimum };
    if (field.maximum !== undefined && value > field.maximum) return { kind: "maximum", limit: field.maximum };
  }
  if (typeof value === "string") {
    const length = [...value].length;
    if (field.minLength !== undefined && length < field.minLength) return { kind: "minLength", limit: field.minLength };
    if (field.maxLength !== undefined && length > field.maxLength) return { kind: "maxLength", limit: field.maxLength };
    if (field.pattern !== undefined) {
      try {
        if (!new RegExp(field.pattern, "u").test(value)) return { kind: "pattern" };
      } catch {
        return null; // a pattern this browser cannot read: the hub checks it
      }
    }
  }
  return null;
}

/** What is wrong with each field of `draft` that holds something; empty when the form can go. */
export function problemsOf(fields: ConfigField[], draft: Draft): Record<string, Problem> {
  const found: Record<string, Problem> = {};
  for (const field of fields) {
    const held = draft[field.name];
    if (typeof held !== "string") {
      if (held?.mode === "set") {
        if (held.value === "") found[field.name] = { kind: "secretEmpty" };
        else {
          const problem = bounds(field, held.value);
          if (problem) found[field.name] = problem;
        }
      }
      continue;
    }
    if (held.trim() === "" && field.kind !== "text") continue;
    if (held === "" && field.kind === "text") continue;
    try {
      const problem = bounds(field, parse(field, held));
      if (problem) found[field.name] = problem;
    } catch (problem) {
      found[field.name] = problem as Problem;
    }
  }
  return found;
}

/**
 * The write that turns `layer` into what `draft` holds: the fields whose value changed, those emptied (unset), and the
 * version the layer is at, so a change made meanwhile is a 409. Null when nothing changed. Call it once
 * `problemsOf` is empty.
 */
export function writeOf(
  fields: ConfigField[],
  layer: Layer,
  draft: Draft,
): { values: Record<string, unknown>; unset: string[]; if_version: number } | null {
  const values: Record<string, unknown> = {};
  const unset: string[] = [];
  for (const field of fields) {
    const held = draft[field.name];
    const isSet = field.kind === "secret" ? layer.secrets.includes(field.name) : field.name in layer.values;
    if (typeof held !== "string") {
      if (held?.mode === "set") values[field.name] = held.value;
      else if (held?.mode === "clear" && isSet) unset.push(field.name);
      continue;
    }
    const empty = field.kind === "text" ? held === "" : held.trim() === "";
    if (empty) {
      if (isSet) unset.push(field.name);
      continue;
    }
    const value = parse(field, held);
    if (!isSet || JSON.stringify(value) !== JSON.stringify(layer.values[field.name])) values[field.name] = value;
  }
  if (Object.keys(values).length === 0 && unset.length === 0) return null;
  return { values, unset, if_version: layer.version };
}
