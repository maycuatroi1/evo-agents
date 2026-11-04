import { createHash, randomBytes } from "node:crypto";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/errors";
import { hashFile, HASH_SLICE, Sha256 } from "@/lib/kg/sha256";
import {
  defaultSource,
  isArchive,
  type KgUploadStarted,
  PARALLEL_PARTS,
  partRanges,
  partUrl,
  sendMissingParts,
} from "@/lib/kg/upload";
import { NEW_PROJECT_LEVELS, uploadLevels } from "@/lib/kg/upload-labels";
import {
  forgetUpload,
  type PendingUpload,
  pendingFor,
  pendingUploads,
  rememberUpload,
} from "@/lib/kg/upload-resume";
import type { Project } from "@/lib/api/client";

import { refusal } from "./archive-drop";
import { bytesSent } from "./use-upload";

const nodeSha = (data: Uint8Array) =>
  createHash("sha256").update(data).digest("hex");

describe("Sha256", () => {
  it("matches node's SHA-256 on the empty input, short inputs and inputs fed in uneven pieces", () => {
    expect(new Sha256().hex()).toBe(
      "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    );
    expect(new Sha256().update(new TextEncoder().encode("abc")).hex()).toBe(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
    for (const size of [55, 56, 63, 64, 65, 1000, 70_001]) {
      const data = new Uint8Array(randomBytes(size));
      const hash = new Sha256();
      for (
        let at = 0, step = 1;
        at < size;
        at += step, step = ((step * 7) % 97) + 1
      )
        hash.update(data.subarray(at, at + step));
      expect(hash.hex(), `${size} bytes`).toBe(nodeSha(data));
    }
  });

  it("hashes a file slice by slice, reporting the bytes read, and stops when cancelled", async () => {
    const data = new Uint8Array(randomBytes(HASH_SLICE + 12_345));
    const read: number[] = [];
    expect(
      await hashFile(
        new Blob([data]),
        (n) => read.push(n),
        new AbortController().signal,
      ),
    ).toBe(nodeSha(data));
    expect(read).toEqual([HASH_SLICE, data.length]);
    const stop = new AbortController();
    stop.abort();
    await expect(
      hashFile(new Blob([data]), () => {}, stop.signal),
    ).rejects.toMatchObject({ name: "AbortError" });
  });
});

describe("archive names", () => {
  it("takes the archives the hub reads and names the source as the hub does", () => {
    expect(["a.zip", "B.TAR", "c.tar.gz", "d.tgz"].map(isArchive)).toEqual([
      true,
      true,
      true,
      true,
    ]);
    expect(["a.rar", "notes.txt", "zip"].map(isArchive)).toEqual([
      false,
      false,
      false,
    ]);
    expect(defaultSource("Lumen Notes (2026).tar.gz")).toBe("Lumen-Notes-2026");
    expect(defaultSource("northwind.handbook.zip")).toBe("northwind.handbook");
    expect(defaultSource("....zip")).toBe("archive");
  });

  it("refuses more than one file, a file that is not an archive, and an empty archive", () => {
    const file = (name: string, size = 3) =>
      new File([new Uint8Array(size)], name);
    expect(refusal([file("a.zip"), file("b.zip")])?.key).toBe("oneFile");
    expect(refusal([])?.key).toBe("oneFile");
    expect(refusal([file("a.pdf")])).toEqual({
      key: "notArchive",
      name: "a.pdf",
    });
    expect(refusal([file("a.zip", 0)])?.key).toBe("empty");
    expect(refusal([file("a.zip")])).toBeNull();
  });
});

describe("parts", () => {
  it("cuts an archive into numbered ranges and counts the bytes sent", () => {
    expect([...partRanges(10, 4)]).toEqual([
      [1, [0, 4]],
      [2, [4, 8]],
      [3, [8, 10]],
    ]);
    expect([...partRanges(8, 4)].length).toBe(2);
    expect(bytesSent({ 1: 4, 2: 3 })).toBe(7);
    expect(partUrl("demo", 7, 2)).toBe("/v1/kg/demo/uploads/7/parts/2");
  });
});

type Sent = { url: string; size: number; headers: Record<string, string> };

/** An XMLHttpRequest that answers each PUT as `answer` says: a status, or "network" for no answer. */
function fakeXhr(answer: (url: string, attempt: number) => number | "network") {
  const sent: Sent[] = [];
  const attempts = new Map<string, number>();
  let inFlight = 0;
  let mostInFlight = 0;
  class FakeXhr {
    upload: { onprogress: ((event: { loaded: number }) => void) | null } = {
      onprogress: null,
    };
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onabort: (() => void) | null = null;
    status = 0;
    responseText = "";
    private url = "";
    private headers: Record<string, string> = {};
    open(_method: string, url: string) {
      this.url = url;
    }
    setRequestHeader(name: string, value: string) {
      this.headers[name] = value;
    }
    getResponseHeader() {
      return null;
    }
    abort() {
      this.onabort?.();
    }
    send(body: Blob) {
      const attempt = (attempts.get(this.url) ?? 0) + 1;
      attempts.set(this.url, attempt);
      inFlight += 1;
      mostInFlight = Math.max(mostInFlight, inFlight);
      setTimeout(() => {
        inFlight -= 1;
        const result = answer(this.url, attempt);
        if (result === "network") return this.onerror?.();
        this.upload.onprogress?.({ loaded: body.size });
        this.status = result;
        this.responseText =
          result >= 400
            ? JSON.stringify({
                error: "refused",
                message: "no",
                request_id: null,
              })
            : "{}";
        if (result < 300)
          sent.push({ url: this.url, size: body.size, headers: this.headers });
        this.onload?.();
      }, 0);
    }
  }
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
  return { sent, attempts, mostInFlight: () => mostInFlight };
}

const started = (over: Partial<KgUploadStarted> = {}): KgUploadStarted => ({
  id: 7,
  source: "lumen",
  state: "uploading",
  size: 10,
  part_size: 2,
  parts: 5,
  done: [],
  resumed: false,
  max_size: 1_000,
  ...over,
});

describe("sendMissingParts", () => {
  afterEach(() => vi.unstubAllGlobals());
  const noWait = () => Promise.resolve();

  it("sends only the parts the store lacks, a few at a time, with the session's headers", async () => {
    const xhr = fakeXhr(() => 200);
    const progress: Array<[number, number]> = [];
    const file = new Blob([new Uint8Array(10)]);
    await sendMissingParts(
      "demo",
      file,
      started({ done: [2, 4] }),
      { "X-Evo-CSRF": "c" },
      (n, s) => progress.push([n, s]),
      new AbortController().signal,
      noWait,
    );
    expect(xhr.sent.map((s) => s.url).sort()).toEqual([
      "/v1/kg/demo/uploads/7/parts/1",
      "/v1/kg/demo/uploads/7/parts/3",
      "/v1/kg/demo/uploads/7/parts/5",
    ]);
    expect(
      xhr.sent.every((s) => s.size === 2 && s.headers["X-Evo-CSRF"] === "c"),
    ).toBe(true);
    expect(xhr.mostInFlight()).toBeLessThanOrEqual(PARALLEL_PARTS);
    expect(
      progress
        .filter(([, s]) => s === 2)
        .map(([n]) => n)
        .sort(),
    ).toEqual([1, 1, 3, 3, 5, 5]);
  });

  it("tries a part again after no answer or a 5xx, and stops on a refusal or after the last try", async () => {
    const xhr = fakeXhr((url, attempt) =>
      url.endsWith("/1") && attempt < 3
        ? attempt === 1
          ? "network"
          : 503
        : 200,
    );
    const waits: number[] = [];
    const wait = (ms: number) => {
      waits.push(ms);
      return Promise.resolve();
    };
    await sendMissingParts(
      "demo",
      new Blob([new Uint8Array(4)]),
      started({ size: 4 }),
      {},
      () => {},
      new AbortController().signal,
      wait,
    );
    expect(xhr.attempts.get("/v1/kg/demo/uploads/7/parts/1")).toBe(3);
    expect(waits).toEqual([1000, 2000]);
    vi.unstubAllGlobals();

    fakeXhr((url) => (url.endsWith("/2") ? 409 : 200));
    const refused = sendMissingParts(
      "demo",
      new Blob([new Uint8Array(4)]),
      started({ size: 4 }),
      {},
      () => {},
      new AbortController().signal,
      noWait,
    );
    await expect(refused).rejects.toBeInstanceOf(ApiError);
    await expect(refused).rejects.toMatchObject({ status: 409 });
    vi.unstubAllGlobals();

    const always = fakeXhr(() => "network");
    await expect(
      sendMissingParts(
        "demo",
        new Blob([new Uint8Array(2)]),
        started({ size: 2 }),
        {},
        () => {},
        new AbortController().signal,
        noWait,
      ),
    ).rejects.toMatchObject({ status: 0 });
    expect(always.attempts.get("/v1/kg/demo/uploads/7/parts/1")).toBe(3);
  });
});

describe("labels an upload may carry", () => {
  const project = (over: Partial<Project>): Project =>
    ({
      name: "demo",
      levels: ["public", "internal", "customer", "secret"],
      default_label: { level: "internal" },
      sinks: [{ id: "hub", kind: "hub", clearance: { level: "customer" } }],
      role: "writer",
      ...over,
    }) as Project;

  it("stops at the hub sink's clearance and picks the default label when it is allowed", () => {
    expect(uploadLevels(project({}))).toEqual({
      kind: "levels",
      levels: ["public", "internal", "customer"],
      preset: "internal",
    });
    expect(
      uploadLevels(project({ default_label: { level: "secret" } })),
    ).toEqual({
      kind: "levels",
      levels: ["public", "internal", "customer"],
      preset: "customer",
    });
    expect(
      uploadLevels(
        project({
          role: "admin",
          sinks: [
            { id: "a", kind: "hub", clearance: { level: "public" } },
          ] as Project["sinks"],
        }),
      ),
    ).toEqual({
      kind: "levels",
      levels: ["public"],
      preset: "public",
    });
  });

  it("offers nothing to a reader or a project with no hub sink", () => {
    expect(uploadLevels(project({ role: "reader" })).kind).toBe("not-writer");
    expect(uploadLevels(project({ sinks: [] })).kind).toBe("no-hub-sink");
    expect(NEW_PROJECT_LEVELS).toEqual([
      "public",
      "internal",
      "customer",
      "secret",
    ]);
  });
});

describe("uploads to resume", () => {
  const record = (over: Partial<PendingUpload> = {}): PendingUpload => ({
    project: "demo",
    id: 1,
    name: "lumen.zip",
    size: 10,
    lastModified: 5,
    sha256: "ab",
    source: "lumen",
    label: "internal",
    startedAt: Date.now(),
    ...over,
  });

  beforeEach(() => {
    const store = new Map<string, string>();
    vi.stubGlobal("window", {
      localStorage: {
        getItem: (key: string) => store.get(key) ?? null,
        setItem: (key: string, value: string) => void store.set(key, value),
      },
      dispatchEvent: () => true,
    });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("keeps a record per project and upload, finds it by name, size and last change, and forgets it", () => {
    rememberUpload(record());
    rememberUpload(
      record({ id: 2, name: "other.zip", startedAt: Date.now() + 1 }),
    );
    rememberUpload(record({ project: "else", id: 3 }));
    rememberUpload(record({ label: "customer" })); // the same upload again: replaced, not added
    expect(pendingUploads("demo").map((r) => r.id)).toEqual([2, 1]);
    expect(
      pendingFor("demo", { name: "lumen.zip", size: 10, lastModified: 5 })
        ?.label,
    ).toBe("customer");
    expect(
      pendingFor("demo", { name: "lumen.zip", size: 10, lastModified: 6 }),
    ).toBeNull();
    forgetUpload("demo", 1);
    expect(pendingUploads("demo").map((r) => r.id)).toEqual([2]);
    expect(pendingUploads("else").map((r) => r.id)).toEqual([3]);
  });

  it("drops records older than a day and anything that is not a record", () => {
    rememberUpload(record({ startedAt: Date.now() - 25 * 60 * 60 * 1000 }));
    expect(pendingUploads("demo")).toEqual([]);
    window.localStorage.setItem(
      "evo.kg-uploads.v1",
      JSON.stringify([{ project: "demo" }, "x"]),
    );
    expect(pendingUploads("demo")).toEqual([]);
    window.localStorage.setItem("evo.kg-uploads.v1", "{not json");
    expect(pendingUploads("demo")).toEqual([]);
  });
});
