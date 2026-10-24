import { useEffect } from "react";

import { characterKeyBlocked } from "@/lib/keyboard";

/**
 * J and K move focus to the next and the previous row of the list (a card's title, a row, a run's row), as mail
 * clients do; Enter then opens what has focus. Single keys, so they leave text fields, dialogs and menus alone and stop
 * when the visitor turned single keys off (`characterKeyBlocked`).
 */
export function useRowKeys() {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.shiftKey || characterKeyBlocked(event)) return;
      const key = event.key.toLowerCase();
      if (key !== "j" && key !== "k") return;
      const rows = [...document.querySelectorAll<HTMLElement>("#main [data-inbox-row]")].filter((row) => row.getClientRects().length > 0);
      if (rows.length === 0) return;
      event.preventDefault();
      const at = rows.findIndex((row) => row === document.activeElement);
      const next = at === -1 ? (key === "j" ? 0 : rows.length - 1) : Math.min(rows.length - 1, Math.max(0, at + (key === "j" ? 1 : -1)));
      rows[next].focus();
      rows[next].scrollIntoView({ block: "nearest" });
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);
}
