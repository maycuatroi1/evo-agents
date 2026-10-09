import { randomInt } from "node:crypto";

import { STACK_URL } from "./env";

/**
 * Telegram as the stack plays it (e2e/hub_stack.py): the hub's bot talks to a fake Bot API, and `pressStart` is the
 * member pressing Start in their private chat on a link the hub made, which reaches the hub's webhook as Telegram's
 * update would, with the secret header.
 */
export function newChatId(): number {
  return randomInt(100_000_000, 2_000_000_000);
}

/** The code of a link `https://t.me/<bot>?start=<code>`. */
export function codeOf(url: string): string {
  const code = new URL(url).searchParams.get("start");
  if (!code) throw new Error(`no start code in ${url}`);
  return code;
}

/** Press Start on the link's code from private chat `chatId`; what the hub did with the update ("linked", ...). */
export async function pressStart(code: string, chatId: number, username: string): Promise<string> {
  const response = await fetch(`${STACK_URL}/telegram/start`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ code, chat_id: chatId, username }),
  });
  if (!response.ok) throw new Error(`hub_stack /telegram/start: ${response.status} ${await response.text()}`);
  const answer = (await response.json()) as { outcome: string };
  return answer.outcome;
}
