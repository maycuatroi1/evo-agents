import { describe, expect, it } from "vitest";

import { type DocVersion, docKey, hubVersion, isDoc, mergeVersions, versionKey } from "./doc-versions";
import type { ViewedFile } from "./outputs";

function file(id: number, name: string, viewer: ViewedFile["viewer"] = "text", source: ViewedFile["source"] = "attached", mediaType = "text/plain"): ViewedFile {
  return { id, source, name, media_type: mediaType, viewer, size: 1, sha256: "", caption: null, created_at: "2026-10-30T00:00:00Z", url: `/f/${id}` };
}

function version(f: ViewedFile, seq: number | null, at = "2026-10-30T00:00:00Z"): DocVersion {
  return { key: versionKey(f), file: f, author: null, at, seq };
}

describe("isDoc", () => {
  it("takes text of every kind and leaves images, PDFs, archives and HTML out", () => {
    expect(isDoc(file(1, "plan.md", "text", "attached", "text/markdown"))).toBe(true);
    expect(isDoc(file(1, "notes.txt"))).toBe(true);
    expect(isDoc(file(1, "deploy.yaml", "text", "attached", "application/yaml"))).toBe(true);
    expect(isDoc(file(1, "data.csv", "text", "attached", "text/csv"))).toBe(true);
    // A YAML file the browser named as bytes is still a document by its name.
    expect(isDoc(file(1, "deploy.yml", "none", "attached", "application/octet-stream"))).toBe(true);
    expect(isDoc(file(1, "shot.png", "image", "attached", "image/png"))).toBe(false);
    expect(isDoc(file(1, "spec.pdf", "pdf", "attached", "application/pdf"))).toBe(false);
    expect(isDoc(file(1, "a.zip", "none", "attached", "application/zip"))).toBe(false);
    expect(isDoc(file(1, "page.html", "html", "attached", "text/html"))).toBe(false);
  });
});

describe("docKey and versionKey", () => {
  it("keys a document by the last part of its name without case, and an attachment and an output apart", () => {
    expect(docKey(" docs/Kế-hoạch.MD ")).toBe("kế-hoạch.md");
    expect(versionKey(file(3, "a.md"))).toBe("attachment-3");
    expect(versionKey(file(3, "a.md", "text", "sent"))).toBe("output-3");
  });
});

describe("mergeVersions", () => {
  it("joins the hub's list and the page's, each once, oldest first by the chat's events", () => {
    const original = version(file(1, "plan.md"), 2);
    const fix = version(file(1, "plan.md", "text", "sent"), 40);
    const streamed = version(file(2, "plan.md", "text", "sent"), 41);
    const merged = mergeVersions([original, fix], [fix, streamed]);
    expect(merged.map((each) => each.key)).toEqual(["attachment-1", "output-1", "output-2"]);
    expect(mergeVersions([], [streamed, original]).map((each) => each.key)).toEqual(["attachment-1", "output-2"]);
  });

  it("orders a run's versions by time and drops what is not a document", () => {
    const late = version(file(1, "deploy.yaml", "text", "sent"), null, "2026-10-30T10:00:00Z");
    const early = version(file(2, "deploy.yaml"), null, "2026-10-30T09:00:00Z");
    const picture = version(file(3, "deploy.yaml", "image", "attached", "image/png"), null, "2026-10-30T08:00:00Z");
    expect(mergeVersions([late], [early, picture]).map((each) => each.key)).toEqual(["attachment-2", "output-1"]);
  });
});

describe("hubVersion", () => {
  it("maps a listed version straight to the file the viewer shows", () => {
    const mapped = hubVersion({
      source: "sent",
      id: 9,
      name: "out/plan.md",
      media_type: "text/markdown",
      viewer: "text",
      size: 12,
      sha256: "d".repeat(64),
      caption: "fixed",
      created_at: "2026-10-30T01:00:00Z",
      url: "/v1/chats/7/outputs/9/content",
      message_id: 4,
      seq: 12,
      thread_root_id: null,
      run_id: 3,
      author: null,
    });
    expect(mapped.key).toBe("output-9");
    expect(mapped.seq).toBe(12);
    expect(mapped.file).toMatchObject({ id: 9, source: "sent", url: "/v1/chats/7/outputs/9/content", caption: "fixed" });
  });
});
