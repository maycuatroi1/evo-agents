import { describe, expect, it } from "vitest";

import { admit, eventFiles, linkedFiles, MAX_ATTACHMENT_BYTES, MAX_ATTACHMENTS, runFiles, viewerOf } from "./attachments";

function file(name: string, size: number): File {
  const made = new File(["x"], name);
  Object.defineProperty(made, "size", { value: size });
  return made;
}

describe("viewerOf", () => {
  it("opens a file as the hub opens an output of its media type", () => {
    expect(viewerOf("image/png")).toBe("image");
    expect(viewerOf("image/svg+xml")).toBe("image");
    expect(viewerOf("video/webm")).toBe("video");
    expect(viewerOf("audio/flac")).toBe("audio");
    expect(viewerOf("application/pdf")).toBe("pdf");
    expect(viewerOf("text/html; charset=utf-8")).toBe("html");
    expect(viewerOf("text/markdown")).toBe("text");
    expect(viewerOf("application/json")).toBe("text");
    expect(viewerOf("application/zip")).toBe("none");
    expect(viewerOf("image/tiff")).toBe("none");
  });
});

describe("admit", () => {
  it("takes files up to the tenth of the message and refuses one over 50 MiB", () => {
    const { taken, refused } = admit([file("a.png", 10), file("big.mov", MAX_ATTACHMENT_BYTES + 1), file("b.txt", MAX_ATTACHMENT_BYTES)], 0);
    expect(taken.map((one) => one.name)).toEqual(["a.png", "b.txt"]);
    expect(refused).toEqual([{ kind: "big", names: ["big.mov"] }]);
  });

  it("refuses the files past the most a message carries", () => {
    const files = Array.from({ length: 4 }, (_, index) => file(`f${index}.txt`, 1));
    const { taken, refused } = admit(files, MAX_ATTACHMENTS - 2);
    expect(taken.map((one) => one.name)).toEqual(["f0.txt", "f1.txt"]);
    expect(refused).toEqual([{ kind: "many", names: ["f2.txt", "f3.txt"] }]);
  });
});

describe("the files of a message", () => {
  it("reads a user_message event's files defensively and links them to the run whose inbox took them", () => {
    const listed = eventFiles([{ id: 4, name: "shot.png", media_type: "image/png", size: 120 }, { name: "no id" }, "x"]);
    expect(listed).toEqual([{ id: 4, name: "shot.png", media_type: "image/png", size: 120 }]);
    expect(eventFiles(undefined)).toEqual([]);
    const [shown] = runFiles(listed, "demo app", 12, "2026-10-23T08:00:00Z");
    expect(shown).toMatchObject({ id: 4, source: "attached", viewer: "image", url: "/v1/projects/demo%20app/runs/12/attachments/4/content", created_at: "2026-10-23T08:00:00Z" });
  });

  it("keeps the URL a chat's message lists", () => {
    const [shown] = linkedFiles([{ id: 2, name: "notes.md", media_type: "text/markdown", size: 9, sha256: "ab", url: "/v1/chats/3/attachments/2/content" }], "2026-10-23T08:00:00Z");
    expect(shown).toMatchObject({ viewer: "text", url: "/v1/chats/3/attachments/2/content", sha256: "ab" });
    expect(linkedFiles(undefined, "2026-10-23T08:00:00Z")).toEqual([]);
  });
});
