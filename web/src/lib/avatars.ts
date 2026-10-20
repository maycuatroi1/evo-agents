import type { Agent } from "@/lib/agents";

/**
 * Faces and the images behind them, apart from the profile rules of lib/agent-rules.ts so that every page drawing an
 * agent's face (the chat list, the Agents pages) loads only this: an avatar's initial and the colour it is written in,
 * and what an image upload takes (an agent's avatar, and a group chat's image, which the hub holds to the same rule).
 */

/** `profiles.COLOR`: an agent's colour, #rrggbb. */
export const COLOR = /^#[0-9a-f]{6}$/;
/** `agent_profiles.AVATAR_MAX_BYTES` and `AVATAR_TYPES`: what PUT .../avatar and PUT /v1/chats/{id}/image take. */
export const AVATAR_MAX_BYTES = 512 * 1024;
export const AVATAR_TYPES = ["image/png", "image/jpeg", "image/webp"] as const;

/** The letter an avatar without an image shows: the name's first letter or digit, else the handle's. */
export function agentInitial(agent: Pick<Agent, "name" | "handle">): string {
  const letter = [...agent.name.trim()].find((char) => /[\p{L}\p{N}]/u.test(char)) ?? agent.handle.charAt(0);
  return letter.toLocaleUpperCase();
}

function channel(value: number): number {
  const c = value / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const n = Number.parseInt(hex.slice(1), 16);
  return 0.2126 * channel((n >> 16) & 255) + 0.7152 * channel((n >> 8) & 255) + 0.0722 * channel(n & 255);
}

const WHITE = "#ffffff";
const INK = "#0e1116"; // the kit's fg in light

/** The colour of an initial on `color`: white or ink, whichever stands out more (WCAG contrast). */
export function inkOn(color: string): string {
  if (!COLOR.test(color)) return WHITE;
  const ground = luminance(color);
  const onWhite = 1.05 / (ground + 0.05);
  const onInk = (ground + 0.05) / (luminance(INK) + 0.05);
  return onWhite >= onInk ? WHITE : INK;
}

/** What is wrong with `file` as an avatar or a chat's image: not a PNG, JPEG or WebP image, or over 512 KiB. */
export function avatarProblem(file: Pick<File, "type" | "size">): "type" | "size" | null {
  if (!(AVATAR_TYPES as readonly string[]).includes(file.type)) return "type";
  if (file.size > AVATAR_MAX_BYTES) return "size";
  return null;
}

/** The bytes of `file` in base64, as PUT .../avatar and PUT /v1/chats/{id}/image take them. */
export async function fileBase64(file: Blob): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  const CHUNK = 0x8000;
  for (let start = 0; start < bytes.length; start += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(start, start + CHUNK));
  }
  return btoa(binary);
}
