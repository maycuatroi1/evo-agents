import { describe, expect, it } from "vitest";

import type { RunOutput } from "@/lib/outputs";

import { buildOutputIndex, sendsFiles, sentFiles } from "./output-index";

const sha = (char: string) => char.repeat(64);

function output(id: number, extra: Partial<RunOutput> = {}): RunOutput {
  return {
    id,
    run_id: 7,
    source: "sent",
    name: `file-${id}.png`,
    media_type: "image/png",
    viewer: "image",
    size: 100 * id,
    sha256: sha(String(id % 10)),
    caption: null,
    tool_call_id: null,
    created_at: "2026-10-21T10:00:10Z",
    url: `/v1/projects/p/runs/7/outputs/${id}/content`,
    ...extra,
  };
}

const at = (second: number) => `2026-10-21T10:00:${String(second).padStart(2, "0")}Z`;
let seq = 0;
const event = (kind: string, body: Record<string, unknown>, second = 1) => ({ seq: ++seq, at: at(second), kind, body });
const link = (hex: string, name = "image.png") => ({ type: "content", content: { type: "resource_link", uri: `evo-output:sha256:${hex}`, name, mimeType: "image/png", size: 10 } });

describe("sentFiles", () => {
  it("reads the files `worker output` and `hub send` name, without their options", () => {
    expect(sentFiles('evo-agents worker output shots/home.png "report 1.md" --caption "Home page, before"')).toEqual(["home.png", "report 1.md"]);
    expect(sentFiles("cd /tmp && evo-agents hub send a.mp4 --session abc --json b.pdf")).toEqual(["a.mp4", "b.pdf"]);
    expect(sentFiles("evo-agents worker output out.png | tee log")).toEqual(["out.png"]);
    expect(sentFiles("evo-agents worker step 3 done")).toEqual([]);
    expect(sentFiles("ls shots")).toEqual([]);
  });

  it("knows the tools that send files", () => {
    expect(sendsFiles({ title: "SendUserFile", arg: null })).toBe(true);
    expect(sendsFiles({ title: "Artifact", arg: "/tmp/page.html" })).toBe(true);
    expect(sendsFiles({ title: "Bash", arg: "evo-agents worker output a.png" })).toBe(true);
    expect(sendsFiles({ title: "Read", arg: "/tmp/a.png" })).toBe(false);
  });
});

describe("buildOutputIndex", () => {
  it("puts each output under its call: by the call's id, by a link's SHA-256, then by a send command's files", () => {
    const outputs = [
      output(1, { source: "tool", tool_call_id: "read-1" }),
      output(2, { source: "tool", name: "image-1a2b3c.png" }),
      output(3, { name: "home.png", created_at: at(20) }),
      output(4, { source: "outputs", name: "shots/later.png" }),
      output(5, { name: "home.png", created_at: at(40) }),
    ];
    const events = [
      event("tool_call", { toolCallId: "read-1", title: "Read", rawInput: { file_path: "/tmp/a.png" } }),
      event("tool_call_update", { toolCallId: "read-1", status: "completed", content: [link(sha("1"))] }),
      event("tool_call", { toolCallId: "shot-1", title: "mcp__browser__screenshot" }),
      // A local session names the image otherwise: the SHA-256 matches it, never the name.
      event("tool_call_update", { toolCallId: "shot-1", status: "completed", content: [link(sha("2"), "browser screenshot.png")] }),
      event("tool_call", { toolCallId: "send-1", title: "Bash", rawInput: { command: "evo-agents worker output home.png" } }, 15),
      event("system", { text: "1 output(s) recorded: home.png", outputs: [3] }, 21),
      event("system", { text: "1 output(s) recorded: shots/later.png", outputs: [4] }, 30),
    ];
    const index = buildOutputIndex(outputs, events);
    expect(index.byCall.get("read-1")?.map((item) => item.id)).toEqual([1]);
    expect(index.byCall.get("shot-1")?.map((item) => item.id)).toEqual([2]);
    // The first home.png after the call is its file; the second, sent later by another means, is not.
    expect(index.byCall.get("send-1")?.map((item) => item.id)).toEqual([3]);
    expect(index.callOf.get(5)).toBeUndefined();
    // The output folder's file shows under the hub's line; the line of a file its call shows is left out.
    expect(index.bySystem.get(events[6].seq)?.map((item) => item.id)).toEqual([4]);
    expect(index.systemOf.get(4)).toBe(events[6].seq);
    expect(index.hiddenSystem.has(events[5].seq)).toBe(true);
  });

  it("prefers the output's own call over a link of another call, and ignores calls the trace does not hold", () => {
    const outputs = [output(1, { tool_call_id: "b" }), output(2, { tool_call_id: "gone" })];
    const events = [
      event("tool_call_update", { toolCallId: "a", content: [link(sha("1"))] }),
      event("tool_call", { toolCallId: "b", title: "SendUserFile", rawInput: { files: ["/tmp/x.png"] } }),
      event("system", { text: "2 output(s) recorded", outputs: [1, 2] }),
    ];
    const index = buildOutputIndex(outputs, events);
    expect(index.callOf.get(1)).toBe("b");
    expect(index.bySystem.get(events[2].seq)?.map((item) => item.id)).toEqual([2]);
    expect(index.hiddenSystem.size).toBe(0);
  });

  it("hides the hub's line of an output deleted since", () => {
    const events = [event("system", { text: "1 output(s) recorded: gone.png", outputs: [9] })];
    expect(buildOutputIndex([], events).hiddenSystem.has(events[0].seq)).toBe(true);
  });
});
