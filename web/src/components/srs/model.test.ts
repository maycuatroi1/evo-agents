import { describe, expect, it } from "vitest";

import type { GraphEdge, GraphNode, NodeDetail } from "@/lib/kg/types";
import { documentOf, ITEMS, item, review, version } from "@/test/fixtures/srs";

import type { SrsComment } from "./api";
import {
  changeCounts,
  factsOf,
  parentUseCase,
  parseHunks,
  proseOutsideTables,
  screensOf,
  screenUsers,
  sectionMarkdown,
  splitCodes,
  stepRefs,
  stepsOf,
} from "./content-model";
import {
  authorsSince,
  codeKey,
  comparePair,
  findItem,
  firstItem,
  kgNodeId,
  outline,
  pathOf,
  policyOf,
  returnedBy,
  reviewBlock,
  rolesOf,
  shownNumber,
  slots,
  threadsOf,
  versionParam,
} from "./model";
import { planStepOf, shortChange, traceOf } from "./trace-model";

const byKey = (key: string) => ITEMS.find((one) => one.key === key)!;

describe("items of a version", () => {
  it("finds an item by key, code or alias as the hub compares codes", () => {
    expect(codeKey("  UC   ord-1 ")).toBe("uc ord-1");
    expect(findItem(ITEMS, "uc1")?.code).toBe("UC ORD-1");
    expect(findItem(ITEMS, "uc  ord-1")?.key).toBe("uc1");
    expect(findItem(ITEMS, "UC-ORD-001")?.key).toBe("uc1");
    expect(findItem(ITEMS, "UC ORD-9")).toBeNull();
  });

  it("gives the knowledge graph's node of a coded item, none for a screen or a section", () => {
    expect(kgNodeId(byKey("uc1"))).toBe("usecase:UC ORD-1");
    expect(kgNodeId(byKey("br1"))).toBe("requirement:BR ORD1-1");
    expect(kgNodeId(byKey("nfr1"))).toBe("requirement:NFR-SEC-01");
    expect(kgNodeId(byKey("cart"))).toBeNull();
    expect(kgNodeId(byKey("purpose"))).toBeNull();
  });

  it("builds the outline of headings, table rows left inside their section", () => {
    const tree = outline(ITEMS);
    expect(tree.map((node) => node.item.key)).toEqual(["approval", "intro", "orders", "screens", "nfrs"]);
    expect(tree[2].children[0].item.key).toBe("uc1");
    expect(tree[2].children[0].children.map((node) => node.item.key)).toEqual(["br1"]);
    expect(tree[4].children).toEqual([]);
    expect(pathOf(ITEMS, "br1").map((above) => above.key)).toEqual(["orders", "uc1"]);
  });

  it("opens the first item that holds text, past the approval page", () => {
    expect(firstItem(ITEMS)?.key).toBe("purpose");
    expect(firstItem([])).toBeNull();
  });
});

describe("versions", () => {
  const doc = documentOf();

  it("shows the version asked when it exists, else the approved one, else the latest", () => {
    expect(shownNumber(doc, 1)).toBe(1);
    expect(shownNumber(doc, 9)).toBe(2);
    expect(shownNumber(doc, null)).toBe(2);
    expect(shownNumber({ ...doc, approved_version: null }, null)).toBe(3);
    expect(versionParam("4")).toBe(4);
    expect(versionParam(["7"])).toBe(7);
    for (const bad of ["0", "-1", "v2", "", undefined, "1.5"]) expect(versionParam(bad)).toBeNull();
  });

  it("compares the pair asked, else the approved version with the latest", () => {
    expect(comparePair(doc, 3, 1)).toEqual({ from: 1, to: 3 });
    expect(comparePair(doc, null, null)).toEqual({ from: 2, to: 3 });
    expect(comparePair(doc, 2, 2)).toEqual({ from: 2, to: 3 });
    expect(comparePair({ ...doc, approved_version: 3 }, null, null)).toEqual({ from: 2, to: 3 });
    expect(comparePair({ ...doc, versions: [version(1)], approved_version: null, latest_version: 1 }, null, null)).toBeNull();
  });

  it("refuses a review as the hub does: readers, a version not in review, its authors, a second review", () => {
    const inReview = doc.versions[0];
    expect([...authorsSince(doc, 3)]).toEqual(["alice"]);
    expect(reviewBlock({ login: "bao", role: "reader" }, doc, inReview)).toBe("reader");
    expect(reviewBlock({ login: "bao", role: "writer" }, doc, doc.versions[1])).toBe("not_in_review");
    expect(reviewBlock({ login: "alice", role: "admin" }, doc, inReview)).toBe("author");
    const reviewed = { ...inReview, reviews: [review({ reviewer: "bao", round: 1 })] };
    expect(reviewBlock({ login: "bao", role: "writer" }, doc, reviewed)).toBe("reviewed");
    expect(reviewBlock({ login: "bao", role: "writer" }, doc, inReview)).toBeNull();
  });

  it("reads the policy and the roles from the effective config, and fills each role with this round's approval", () => {
    const fields = [
      { name: "approval_policy", value: { srs: { required: ["ba", "qa"], optional: ["dev", "nobody"] } } },
      { name: "roles", value: { ba: ["alice"], qa: ["bao", "chi"], dev: ["bao"] } },
    ];
    const policy = policyOf(fields, "srs");
    expect(policy).toEqual({ required: ["ba", "qa"], optional: ["dev"] });
    expect(policyOf(fields, "spec")).toEqual({ required: [], optional: [] });
    expect(rolesOf(fields, "bao")).toEqual(["qa", "dev"]);
    expect(rolesOf([], "bao")).toEqual([]);
    const held = version(3, { state: "in_review", round: 2, reviews: [review({ reviewer: "bao", role: "qa", round: 2 }), review({ reviewer: "chi", role: "ba", round: 1 })] });
    expect(slots(policy, held).map((slot) => [slot.role, slot.required, slot.by?.reviewer ?? null])).toEqual([
      ["ba", true, null],
      ["qa", true, "bao"],
      ["dev", false, null],
    ]);
  });

  it("tells the request for changes that sent a draft back", () => {
    const back = version(4, { state: "draft", round: 1, reviews: [review({ decision: "request_changes", body: "Fix step 2" })] });
    expect(returnedBy(back)?.body).toBe("Fix step 2");
    expect(returnedBy(version(4))).toBeNull();
  });
});

describe("what an item holds", () => {
  it("reads a use case's fields in the template's order, then the others", () => {
    expect(factsOf(byKey("uc1")).map((fact) => [fact.name, fact.known])).toEqual([
      ["id", true],
      ["objective", true],
      ["actor", true],
      ["priority", false],
    ]);
    expect(stepsOf(byKey("uc1"))).toEqual({ columns: ["step", "actor", "system"], rows: byKey("uc1").fields.steps });
    expect(stepRefs(byKey("br1"))).toEqual(["2"]);
  });

  it("finds the screens of a use case, linked or named, and the use cases of a screen", () => {
    expect(screensOf(ITEMS, byKey("uc1")).map(({ screen, linked }) => [screen.key, linked])).toEqual([["cart", true]]);
    const named = { ...byKey("uc1"), refs: [] };
    expect(screensOf(ITEMS, named).map(({ screen, linked }) => [screen.key, linked])).toEqual([["cart", false]]);
    expect(screenUsers(ITEMS, byKey("cart")).map((one) => one.key)).toEqual(["uc1"]);
    expect(screenUsers(ITEMS, byKey("checkout"))).toEqual([]);
    expect(parentUseCase(ITEMS, byKey("br1"))?.key).toBe("uc1");
    expect(parentUseCase(ITEMS, byKey("purpose"))).toBeNull();
  });

  it("reads a section with its rows and the text after its table, and a use case's prose outside its tables", () => {
    expect(sectionMarkdown(ITEMS, byKey("nfrs"))).toBe(
      "| ID | Group | Requirement |\n|---|---|---|\n| NFR-SEC-01 | Security | Card data stays out |\n\nEach is checked before a release.",
    );
    expect(proseOutsideTables(byKey("uc1").body)).toBe("The cart is kept for a day.");
    expect(proseOutsideTables("```\n| kept |\n```")).toBe("```\n| kept |\n```");
  });

  it("cuts a line into text and the codes it names, the longest first, by alias too", () => {
    expect(splitCodes("Checks the cart (BR ORD1-1) per UC-ORD-001", ITEMS)).toEqual([
      "Checks the cart (",
      { code: "BR ORD1-1", ref: "BR ORD1-1" },
      ") per ",
      { code: "UC-ORD-001", ref: "UC ORD-1" },
    ]);
    expect(splitCodes("XBR ORD1-1 is not a code", ITEMS)).toEqual(["XBR ORD1-1 is not a code"]);
    expect(splitCodes("", ITEMS)).toEqual([]);
  });

  it("reads a unified diff into numbered lines, and counts changes", () => {
    const hunks = parseHunks(["@@ -3,2 +3,3 @@", " same", "-old", "+new", "+more"]);
    expect(hunks).toEqual([
      {
        oldStart: 3,
        newStart: 3,
        lines: [
          { kind: "context", text: "same", old: 3, new: 3 },
          { kind: "removed", text: "old", old: 4, new: null },
          { kind: "added", text: "new", old: null, new: 4 },
          { kind: "added", text: "more", old: null, new: 5 },
        ],
      },
    ]);
    const change = (kind: "added" | "changed" | "removed" | "moved") => ({ key: kind, kind: "section" as const, code: null, change: kind, title_from: null, title_to: null, lines: [] });
    expect(changeCounts([change("added"), change("changed"), change("changed"), change("moved")])).toEqual({ added: 1, changed: 2, removed: 0, moved: 1 });
  });
});

describe("the trace of an item in the knowledge graph", () => {
  const label = { level: "internal", location: "any", integrity: "U" };
  const node = (id: string, kind: string, props: Record<string, unknown> = {}): GraphNode => ({
    id,
    kind,
    name: id,
    status: "declared",
    conf: 1,
    label,
    props,
    aliases: [],
    hop: 1,
    hub: false,
  });
  const edge = (src: string, rel: string, dst: string): GraphEdge => ({ id: `${src}-${rel}-${dst}`, src, rel, dst, status: "declared" });

  it("follows tasks to the item and its rules, their plan steps, the steps' commits, and what verifies it", () => {
    const focus = "usecase:UC ORD-1";
    const hood = {
      focus,
      nodes: [
        node(focus, "UseCase"),
        node("requirement:BR ORD1-1", "Requirement"),
        node("plugin.tasks:ticket:T-1", "Ticket", { status: "done" }),
        node("plugin.tasks:ticket:T-2", "Ticket"),
        node("plugin.tasks:ticket:T-9", "Ticket"),
        node("plan:rollout/step:1", "PlanStep", { status: "done" }),
        node("tests/test_cart.py", "File"),
        node("doc#section", "Section"),
      ],
      edges: [
        edge("requirement:BR ORD1-1", "child_of", focus),
        edge("plugin.tasks:ticket:T-1", "implements", focus),
        edge("plugin.tasks:ticket:T-2", "implements", "requirement:BR ORD1-1"),
        edge("plan:rollout/step:1", "implements", "plugin.tasks:ticket:T-1"),
        edge("tests/test_cart.py", "verifies", focus),
        edge("doc#section", "defines", focus),
      ],
    };
    const detail = {
      outgoing: [{ rel: "mentions", node: "commit:shop@4f1c2e7a9b3d", kind: "Commit", name: null, status: "parsed", conf: 1 }],
    } as unknown as NodeDetail;
    const trace = traceOf(hood, [detail]);
    expect(trace.tickets.map((one) => [one.id, one.status])).toEqual([
      ["plugin.tasks:ticket:T-1", "done"],
      ["plugin.tasks:ticket:T-2", null],
    ]);
    expect(trace.steps.map((one) => one.id)).toEqual(["plan:rollout/step:1"]);
    expect(trace.changes.map((one) => one.id)).toEqual(["commit:shop@4f1c2e7a9b3d"]);
    expect(trace.tests.map((one) => one.id)).toEqual(["tests/test_cart.py"]);
    expect(planStepOf("plan:rollout/step:1")).toEqual({ plan: "rollout", step: "1" });
    expect(planStepOf("usecase:UC ORD-1")).toBeNull();
    expect(shortChange(trace.changes[0])).toBe("shop@4f1c2e7");
  });
});

describe("comments", () => {
  const comment = (id: number, parent: number | null, patch: Partial<SrsComment> = {}): SrsComment => ({
    id,
    version: 2,
    item: "uc1",
    item_code: "UC ORD-1",
    parent_id: parent,
    author: "bao",
    run_id: null,
    body: `comment ${id}`,
    created_at: "2026-10-10T09:00:00Z",
    resolved_by: null,
    resolved_at: null,
    ...patch,
  });

  it("groups answers under their thread, answers to answers included", () => {
    const threads = threadsOf([comment(1, null), comment(2, 1), comment(3, null, { resolved_by: "alice" }), comment(4, 2)]);
    expect(threads.map((thread) => [thread.root.id, thread.replies.map((reply) => reply.id)])).toEqual([
      [1, [2, 4]],
      [3, []],
    ]);
  });
});

describe("fixtures", () => {
  it("builds items with defaults", () => {
    expect(item("x").kind).toBe("section");
  });
});
