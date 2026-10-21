import { describe, expect, it } from "vitest";

import { draftOf, fieldsOf, problemsOf, type SecretDraft, textOf, writeOf } from "./config-model";

const SCHEMA = {
  type: "object",
  properties: {
    greeting: { type: "string", title: "Greeting", default: "hello", maxLength: 10, pattern: "^[a-z ]+$" },
    retries: { type: "integer", minimum: 0, maximum: 5, default: 1 },
    ratio: { type: "number" },
    channel: { enum: ["general", "ops"] },
    loud: { type: "boolean", description: "Shout" },
    tags: { type: "array", items: { type: "string" } },
    extra: { type: "object" },
    token: { type: "string", "x-secret": true, minLength: 8 },
  },
  required: ["greeting", "token"],
};

const LAYER = { values: { greeting: "hi", retries: 2, loud: false, tags: ["a", "b"] }, secrets: ["token"], version: 3 };

describe("plugin config form", () => {
  it("reads each field of the schema with its kind, title, bounds and default", () => {
    const fields = fieldsOf(SCHEMA);
    // required first, secrets last, by title within each: the hub's JSONB keeps no order of the author's
    expect(fields.map((field) => [field.name, field.kind])).toEqual([
      ["greeting", "text"],
      ["channel", "choice"],
      ["extra", "json"],
      ["loud", "boolean"],
      ["ratio", "number"],
      ["retries", "integer"],
      ["tags", "list"],
      ["token", "secret"],
    ]);
    const byName = Object.fromEntries(fields.map((field) => [field.name, field]));
    const { greeting, retries, channel, loud, token } = byName;
    expect(greeting).toMatchObject({ title: "Greeting", required: true, fallback: "hello", maxLength: 10 });
    expect(retries).toMatchObject({ title: "retries", minimum: 0, maximum: 5, fallback: "1", required: false });
    expect(channel.choices).toEqual(["general", "ops"]);
    expect(loud).toMatchObject({ choices: [true, false], description: "Shout" });
    expect(token).toMatchObject({ required: true, fallback: null });
    expect(fieldsOf(null)).toEqual([]);
  });

  it("starts from what the layer sets, secrets kept and never shown", () => {
    const fields = fieldsOf(SCHEMA);
    expect(draftOf(fields, LAYER)).toEqual({
      greeting: "hi",
      retries: "2",
      ratio: "",
      channel: "",
      loud: "false",
      tags: "a\nb",
      extra: "",
      token: { mode: "keep", value: "" },
    });
    expect(textOf("json", { a: 1 })).toBe('{\n  "a": 1\n}');
  });

  it("checks what a field holds the way the hub checks one layer", () => {
    const fields = fieldsOf(SCHEMA);
    const draft = {
      ...draftOf(fields, LAYER),
      greeting: "Hello world, again",
      retries: "1.5",
      ratio: "abc",
      extra: "{",
      token: { mode: "set", value: "" } satisfies SecretDraft,
    };
    expect(problemsOf(fields, draft)).toEqual({
      greeting: { kind: "maxLength", limit: 10 },
      retries: { kind: "integer" },
      ratio: { kind: "number" },
      extra: { kind: "json" },
      token: { kind: "secretEmpty" },
    });
    expect(problemsOf(fields, { ...draft, greeting: "HI", retries: "9", ratio: "", extra: "", token: { mode: "set", value: "short" } })).toEqual({
      greeting: { kind: "pattern" },
      retries: { kind: "maximum", limit: 5 },
      token: { kind: "minLength", limit: 8 },
    });
    expect(problemsOf(fields, draftOf(fields, LAYER))).toEqual({});
  });

  it("writes only what changed, unsets what was emptied, and names the layer's version", () => {
    const fields = fieldsOf(SCHEMA);
    const kept = draftOf(fields, LAYER);
    expect(writeOf(fields, LAYER, kept)).toBeNull();
    const changed = {
      ...kept,
      greeting: "",
      retries: "4",
      channel: '"ops"',
      loud: "",
      tags: " a \n\n c ",
      token: { mode: "set", value: "s3cret-value" } satisfies SecretDraft,
    };
    expect(writeOf(fields, LAYER, changed)).toEqual({
      values: { retries: 4, channel: "ops", tags: ["a", "c"], token: "s3cret-value" },
      unset: ["greeting", "loud"],
      if_version: 3,
    });
    const cleared = { ...kept, token: { mode: "clear", value: "" } satisfies SecretDraft };
    expect(writeOf(fields, LAYER, cleared)).toEqual({ values: {}, unset: ["token"], if_version: 3 });
    const notHeld = { ...LAYER, secrets: [] };
    expect(writeOf(fields, notHeld, cleared)).toBeNull(); // clearing what the layer does not set changes nothing
  });
});
