import { describe, expect, it } from "vitest";

import {
  bootstrapCommand,
  BOOTSTRAP_URL,
  emptyPoolForm,
  expiringSoon,
  inheritedOf,
  nodeProblem,
  ownOf,
  parseList,
  POOL_LABEL,
  poolChanges,
  poolForm,
  poolHealth,
  readPoolForm,
  TAINT,
  userDataCommand,
  words,
} from "./pool-model";
import type { Pool, PoolNode } from "./queries";

const NOW = Date.parse("2026-10-25T12:00:00Z");

const node = (patch: Partial<PoolNode> = {}): PoolNode => ({
  id: 1,
  name: "lab-1",
  hostname: "lab-1",
  status: "online",
  agent_version: "1.0.0",
  labels: [],
  ready: true,
  ready_reason: null,
  created_at: "2026-10-01T00:00:00Z",
  last_heartbeat_at: "2026-10-25T11:59:00Z",
  ...patch,
});

const POOL: Pool = {
  id: 7,
  name: "lab",
  owner: "ana",
  projects: ["docs", "demo"],
  slots: 2,
  labels: ["gpu"],
  taints: ["heavy"],
  accepted_findings: [],
  reap_after_days: 7,
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
  nodes: [],
};

const form = (patch = {}) => ({ ...emptyPoolForm(), name: "lab", projects: ["demo"], ...patch });

describe("the words of a list field", () => {
  it("splits at commas, spaces and line breaks, keeps each once, and lowercases only when asked", () => {
    expect(words("GPU, gpu\nzone=a  zone=a")).toEqual(["GPU", "gpu", "zone=a"]);
    expect(words("GPU, gpu", true)).toEqual(["gpu"]);
  });

  it("parts the words the pattern refuses and those with a key the hub sets itself", () => {
    expect(parseList("gpu, Pool=x, -bad, os=linux", POOL_LABEL, 16, { reserve: true })).toEqual({
      values: ["gpu"],
      invalid: ["-bad"],
      reserved: ["Pool=x", "os=linux"],
      tooMany: false,
    });
    expect(parseList("a b c", TAINT, 2).tooMany).toBe(true);
  });
});

describe("the pool form", () => {
  it("reads a pool with sorted projects and the lists as typed", () => {
    expect(readPoolForm(form({ projects: ["docs", "demo"], slots: "3", labels: "GPU zone=a", taints: "heavy" }), 16)).toEqual({
      pool: { name: "lab", projects: ["demo", "docs"], slots: 3, labels: ["GPU", "zone=a"], taints: ["heavy"], accepted_findings: [], reap_after_days: 7 },
      errors: null,
    });
  });

  it("says the first problem of each field", () => {
    const read = readPoolForm(form({ name: "-x", projects: [], slots: "17", labels: "pool=a", accepted: "Bad", reapDays: "0" }), 16);
    expect(read.errors).toEqual({
      name: { code: "nameInvalid" },
      projects: { code: "projectsRequired" },
      slots: { code: "number", min: 1, max: 16 },
      labels: { code: "reserved", values: "pool=a" },
      accepted: { code: "invalid", values: "Bad" },
      reapDays: { code: "number", min: 1, max: 90 },
    });
    expect(readPoolForm(form({ name: " " }), 16).errors).toEqual({ name: { code: "nameRequired" } });
  });

  it("sends only what a save changes, and nothing when the form is the pool", () => {
    const same = readPoolForm(poolForm(POOL), 16);
    expect(same.pool && poolChanges(POOL, same.pool)).toBeNull();
    const next = readPoolForm({ ...poolForm(POOL), slots: "4", taints: "" }, 16);
    expect(next.pool && poolChanges(POOL, next.pool)).toEqual({ slots: 4, taints: [] });
  });
});

describe("what about a pool needs a person", () => {
  it("names a node offline, draining or held back by its doctor", () => {
    expect(nodeProblem(node())).toBeNull();
    expect(nodeProblem(node({ ready: false }))).toBe("notReady");
    expect(nodeProblem(node({ status: "offline", ready: false }))).toBe("offline");
    expect(nodeProblem(node({ status: "draining" }))).toBe("draining");
  });

  it("counts an offline node once, as offline, and a draining one as not taking runs", () => {
    const nodes = [node(), node({ id: 2, ready: false }), node({ id: 3, status: "offline", ready: false }), node({ id: 4, status: "draining" })];
    expect(poolHealth({ nodes })).toEqual({ nodes: 4, ready: 1, notReady: 1, offline: 1 });
  });

  it("says a live token expires soon within two hours of its end only", () => {
    const at = (ms: number) => new Date(NOW + ms).toISOString();
    expect(expiringSoon({ status: "live", expires_at: at(60 * 60 * 1000) }, NOW)).toBe(true);
    expect(expiringSoon({ status: "live", expires_at: at(3 * 60 * 60 * 1000) }, NOW)).toBe(false);
    expect(expiringSoon({ status: "revoked", expires_at: at(60 * 60 * 1000) }, NOW)).toBe(false);
    expect(expiringSoon({ status: "live", expires_at: at(-1) }, NOW)).toBe(false);
    expect(expiringSoon({ status: "live", expires_at: at(60 * 60 * 1000) }, null)).toBe(false);
  });
});

describe("the commands of a join token", () => {
  it("downloads the bootstrap script and runs it with the hub and the token, quoted when they need it", () => {
    expect(bootstrapCommand("https://hub.example.org", "evj_abc")).toBe(
      `curl -fsSL ${BOOTSTRAP_URL} -o bootstrap.sh && EVO_HUB_URL=https://hub.example.org EVO_JOIN_TOKEN=evj_abc bash bootstrap.sh`,
    );
    expect(bootstrapCommand("https://hub", "a b")).toContain("EVO_JOIN_TOKEN='a b' bash");
    expect(userDataCommand("lab")).toBe("evo-agents worker pool user-data lab > user-data.sh");
  });
});

describe("a node's own labels and taints", () => {
  it("takes the pool's off what the hub shows", () => {
    const inherited = inheritedOf(POOL);
    expect(inherited).toEqual({ labels: ["gpu", "pool=lab"], taints: ["heavy"] });
    expect(ownOf({ labels: ["fast", "gpu", "pool=lab"], taints: ["mine", "heavy"] }, inherited)).toEqual({ labels: ["fast"], taints: ["mine"] });
    expect(ownOf({ labels: ["fast"], taints: ["mine"] }, null)).toEqual({ labels: ["fast"], taints: ["mine"] });
  });
});
