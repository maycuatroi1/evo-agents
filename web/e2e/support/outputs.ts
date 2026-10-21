import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { crc32, deflateSync } from "node:zlib";

import type { components } from "../../src/lib/api/schema";

import { API_URL } from "./env";
import { type Account, machineToken } from "./hub";
import type { LiveWorker } from "./runs";
import { PROTOCOL_HEADER } from "./workers";

/**
 * The outputs of a run as its machine sends them (docs/hub.md, Run outputs): the uploads of the bytes the project does
 * not hold yet (presigned PUTs to the stack's moto bucket), then the outputs that commit them, with the worker's token
 * for a worker's run and the member's for a local session. And files of each kind the web shows, made here: images
 * drawn as a small page would look, a WAV tone, a one-page PDF, text, an HTML page that tries to reach the hub, and the
 * fixture video (e2e/fixtures/clip.webm, three seconds of ffmpeg's testsrc2 in VP8, which Chromium plays).
 */
export type RunOutput = components["schemas"]["RunOutput"];
export type OutputFile = {
  name: string;
  media_type: string;
  data: Buffer;
  source?: RunOutput["source"];
  caption?: string;
  tool_call_id?: string;
};

export const sha256 = (data: Buffer) => createHash("sha256").update(data).digest("hex");

async function post(token: string, url: string, body: unknown, worker: boolean): Promise<unknown> {
  const response = await fetch(`${API_URL}${url}`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${token}`, ...(worker ? PROTOCOL_HEADER : {}) },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`POST ${url}: ${response.status} ${await response.text()}`);
  return response.json();
}

async function send(token: string, base: string, files: OutputFile[], worker: boolean): Promise<RunOutput[]> {
  const items = files.map((file) => ({ sha256: sha256(file.data), size: file.data.length, source: file.source ?? "sent" }));
  const asked = (await post(token, `${base}/output-uploads`, { items }, worker)) as components["schemas"]["Uploads"];
  const tickets = new Map(asked.uploads.map((ticket) => [ticket.sha256, ticket]));
  for (const file of files) {
    const ticket = tickets.get(sha256(file.data));
    if (!ticket) continue;
    const put = await fetch(ticket.url, { method: "PUT", body: new Uint8Array(file.data), headers: { "content-type": "application/octet-stream" } });
    if (!put.ok) throw new Error(`PUT ${file.name} to the presigned URL: ${put.status}`);
  }
  const outputs = files.map((file) => ({
    sha256: sha256(file.data),
    size: file.data.length,
    source: file.source ?? "sent",
    name: file.name,
    media_type: file.media_type,
    caption: file.caption ?? null,
    tool_call_id: file.tool_call_id ?? null,
    upload_id: tickets.get(sha256(file.data))?.upload_id ?? null,
  }));
  const recorded = (await post(token, `${base}/outputs`, { outputs }, worker)) as components["schemas"]["RecordedOutputs"];
  return recorded.outputs;
}

/** A worker's run sends `files` (`evo-agents worker output`, tool images, .evo-run/outputs/), as its daemon does. */
export function sendOutputs(live: LiveWorker, runId: number, files: OutputFile[]): Promise<RunOutput[]> {
  return send(live.token, `/v1/worker/runs/${runId}`, files, true);
}

/** A member's local session sends `files` to its run (SendUserFile, a tool's image), with the member's token. */
export async function sendLocalOutputs(owner: Account, project: string, runId: number, files: OutputFile[]): Promise<RunOutput[]> {
  return send(await machineToken(owner), `/v1/projects/${project}/local-runs/${runId}`, files, false);
}

/** The ACP content that stands in a tool's event for an image taken out of its result. */
export function outputLink(file: OutputFile) {
  return { type: "content", content: { type: "resource_link", uri: `evo-output:sha256:${sha256(file.data)}`, name: file.name, mimeType: file.media_type, size: file.data.length } };
}

// Files of each kind.

type Rgb = [number, number, number];

function chunk(type: string, data: Buffer): Buffer {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body) >>> 0);
  return Buffer.concat([length, body, crc]);
}

/**
 * A PNG of `width` by `height` that reads as a screenshot of a small page: a bar across the top in `accent`, a column
 * of lines on the left and cards in a grid, on a light ground; `dark` swaps the ground.
 */
export function pageShot(width: number, height: number, accent: Rgb, dark = false): Buffer {
  const ground: Rgb = dark ? [24, 24, 27] : [250, 250, 250];
  const ink: Rgb = dark ? [63, 63, 70] : [228, 228, 231];
  const pixels = Buffer.alloc(height * (width * 3 + 1));
  const fill = (x0: number, y0: number, w: number, h: number, [r, g, b]: Rgb) => {
    for (let y = Math.max(0, y0); y < Math.min(height, y0 + h); y += 1) {
      for (let x = Math.max(0, x0); x < Math.min(width, x0 + w); x += 1) {
        const at = y * (width * 3 + 1) + 1 + x * 3;
        pixels[at] = r;
        pixels[at + 1] = g;
        pixels[at + 2] = b;
      }
    }
  };
  fill(0, 0, width, height, ground);
  fill(0, 0, width, Math.round(height * 0.12), accent);
  const pad = Math.round(width * 0.04);
  const side = Math.round(width * 0.22);
  for (let line = 0; line < 6; line += 1) fill(pad, Math.round(height * 0.2) + line * Math.round(height * 0.09), side - pad, Math.round(height * 0.04), ink);
  const cardW = Math.round((width - side - 3 * pad) / 2);
  const cardH = Math.round(height * 0.3);
  for (let row = 0; row < 2; row += 1) {
    for (let column = 0; column < 2; column += 1) {
      const x = side + pad + column * (cardW + pad);
      const y = Math.round(height * 0.2) + row * (cardH + pad);
      fill(x, y, cardW, cardH, ink);
      fill(x + pad / 2, y + pad / 2, Math.round(cardW * 0.5), Math.round(cardH * 0.12), accent);
    }
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8; // bit depth
  header[9] = 2; // RGB
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk("IHDR", header), chunk("IDAT", deflateSync(pixels)), chunk("IEND", Buffer.alloc(0))]);
}

/** One second of a 440 Hz tone, 8 kHz mono 16-bit PCM. */
export function tone(): Buffer {
  const rate = 8000;
  const samples = rate;
  const data = Buffer.alloc(samples * 2);
  for (let index = 0; index < samples; index += 1) data.writeInt16LE(Math.round(Math.sin((2 * Math.PI * 440 * index) / rate) * 8000), index * 2);
  const head = Buffer.alloc(44);
  head.write("RIFF", 0, "ascii");
  head.writeUInt32LE(36 + data.length, 4);
  head.write("WAVEfmt ", 8, "ascii");
  head.writeUInt32LE(16, 16);
  head.writeUInt16LE(1, 20);
  head.writeUInt16LE(1, 22);
  head.writeUInt32LE(rate, 24);
  head.writeUInt32LE(rate * 2, 28);
  head.writeUInt16LE(2, 32);
  head.writeUInt16LE(16, 34);
  head.write("data", 36, "ascii");
  head.writeUInt32LE(data.length, 40);
  return Buffer.concat([head, data]);
}

/** A one-page PDF that says `text` in Helvetica. */
export function onePagePdf(text: string): Buffer {
  const stream = `BT /F1 24 Tf 72 720 Td (${text}) Tj ET`;
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
    `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`,
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
  ];
  let body = "%PDF-1.4\n";
  const offsets: number[] = [];
  objects.forEach((object, index) => {
    offsets.push(body.length);
    body += `${index + 1} 0 obj\n${object}\nendobj\n`;
  });
  const xref = body.length;
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets.map((offset) => `${String(offset).padStart(10, "0")} 00000 n \n`).join("")}`;
  body += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(body, "latin1");
}

/**
 * An HTML page whose script tries what a page an agent made must never do with the hub's origin: read the visitor's
 * cookies and call the hub's API. It writes what happened into its body, where the spec reads it through the frame.
 */
export const PROBE_HTML = Buffer.from(
  `<!doctype html><html><body><h1>Probe of northwind</h1><p id="cookie">reading</p><p id="api">calling</p><script>
try { document.getElementById("cookie").textContent = "cookie:" + document.cookie; } catch (error) { document.getElementById("cookie").textContent = "cookie blocked"; }
fetch("/v1/auth/whoami", { credentials: "include" }).then((answer) => answer.text()).then((text) => { document.getElementById("api").textContent = "api answered: " + text.slice(0, 40); }).catch(() => { document.getElementById("api").textContent = "api blocked"; });
</script></body></html>`,
);

export const CLIP_WEBM = readFileSync(new URL("../fixtures/clip.webm", import.meta.url));

/** One file of each kind the viewer shows, plus one it offers only to download. */
export function filesOfEachKind(): OutputFile[] {
  return [
    { name: "shots/cart.png", media_type: "image/png", data: pageShot(960, 600, [47, 75, 216]), source: "outputs" },
    { name: "checkout.webm", media_type: "video/webm", data: CLIP_WEBM, caption: "The checkout, recorded with the fix" },
    { name: "chime.wav", media_type: "audio/wav", data: tone() },
    { name: "release-notes.pdf", media_type: "application/pdf", data: onePagePdf("Release notes of northwind") },
    { name: "report.md", media_type: "text/markdown", data: Buffer.from("# Checkout findings\n\nThe cart total is **right** now.\n\n- 3 tests added\n- 0 failed\n") },
    { name: "results.json", media_type: "application/json", data: Buffer.from('{"passed":42,"failed":0,"suite":"checkout"}') },
    { name: "timings.csv", media_type: "text/csv", data: Buffer.from("step,seconds\nlogin,1.2\ncart,0.8\npay,2.4\n") },
    { name: "fix_total.py", media_type: "text/x-python", data: Buffer.from("def total(items):\n    return sum(item.price for item in items)\n") },
    { name: "probe.html", media_type: "text/html", data: PROBE_HTML },
    { name: "bundle.zip", media_type: "application/zip", data: Buffer.from("PK\u0003\u0004 not a real archive of lumen") },
  ];
}
