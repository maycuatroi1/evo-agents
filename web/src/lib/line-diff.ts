/**
 * A line diff of two texts, as git shows it: split on line ends (\n or \r\n, a final line end adds no empty line),
 * the common head and tail trimmed, the middle matched by Myers' shortest edit script. Within each changed block the
 * removed lines come before the added ones. A middle whose search would keep over MAX_CELLS diagonal entries, or
 * compare over MAX_STEPS pairs of items along them, is shown whole as removed then added, so a huge rewrite stays fast.
 */

export type DiffKind = "same" | "add" | "del";
export type DiffLine = { kind: DiffKind; text: string; oldNo: number | null; newNo: number | null };

export const MAX_CELLS = 4_000_000;
/** The comparisons one search may make following its diagonals: a few tens of milliseconds. */
export const MAX_STEPS = 20_000_000;

function splitLines(text: string): string[] {
  if (text === "") return [];
  const lines = text.split(/\r?\n/);
  if (lines[lines.length - 1] === "") lines.pop();
  return lines;
}

/** Strings as integers, one id per distinct string across both sides, so the search compares numbers. */
export function toIds(a: readonly string[], b: readonly string[]): [Int32Array, Int32Array] {
  const ids = new Map<string, number>();
  const toId = (item: string) => {
    let id = ids.get(item);
    if (id === undefined) {
      id = ids.size;
      ids.set(item, id);
    }
    return id;
  };
  return [Int32Array.from(a, toId), Int32Array.from(b, toId)];
}

/**
 * The shortest edit script of two sequences of ids as kinds: "same" consumes an item of both sides, "del" of the old,
 * "add" of the new. Null when the search would keep over `maxCells` diagonal entries or make over `maxSteps`
 * comparisons. The word diff (word-diff.ts) runs it on the words of a changed line.
 */
export function editScript(
  ai: Int32Array,
  bi: Int32Array,
  maxCells: number = MAX_CELLS,
  /** Entries (and comparisons, in `steps`) left to several searches together: this one takes off what it spent. */
  budget?: { left: number; steps?: number },
  maxSteps: number = MAX_STEPS,
): DiffKind[] | null {
  const n = ai.length;
  const m = bi.length;
  if (n === 0) return new Array<DiffKind>(m).fill("add");
  if (m === 0) return new Array<DiffKind>(n).fill("del");

  // Myers' greedy O(ND) search, keeping each step's furthest-reaching x per diagonal (k in -(d+1)..d+1) to walk back.
  const offset = n + m + 1;
  const v = new Int32Array(2 * offset + 1);
  const trace: Int32Array[] = [];
  let cells = 0;
  let steps = 0;
  let found = -1;
  const spend = () => {
    if (!budget) return;
    budget.left -= cells;
    if (budget.steps !== undefined) budget.steps -= steps;
  };
  for (let d = 0; d <= n + m && found < 0; d++) {
    for (let k = -d; k <= d; k += 2) {
      let x = k === -d || (k !== d && v[offset + k - 1] < v[offset + k + 1]) ? v[offset + k + 1] : v[offset + k - 1] + 1;
      let y = x - k;
      const from = x;
      while (x < n && y < m && ai[x] === bi[y]) {
        x++;
        y++;
      }
      steps += x - from + 1;
      if (steps > maxSteps) {
        spend();
        return null;
      }
      v[offset + k] = x;
      if (x >= n && y >= m) found = d;
    }
    trace.push(v.slice(offset - d - 1, offset + d + 2));
    cells += 2 * d + 3;
    if (found < 0 && cells > maxCells) {
      spend();
      return null;
    }
  }
  spend();

  const reversed: DiffKind[] = [];
  let x = n;
  let y = m;
  for (let d = found; d > 0; d--) {
    const prev = trace[d - 1];
    const at = (k: number) => prev[k + d];
    const k = x - y;
    const prevK = k === -d || (k !== d && at(k - 1) < at(k + 1)) ? k + 1 : k - 1;
    const prevX = at(prevK);
    const prevY = prevX - prevK;
    while (x > prevX && y > prevY) {
      reversed.push("same");
      x--;
      y--;
    }
    reversed.push(x === prevX ? "add" : "del");
    x = prevX;
    y = prevY;
  }
  while (x-- > 0) reversed.push("same");
  return reversed.reverse();
}

/** The middle's edit script; a middle too big to search is shown whole as removed then added. */
function middleScript(a: string[], b: string[]): DiffKind[] {
  const [ai, bi] = toIds(a, b);
  return editScript(ai, bi) ?? [...new Array<DiffKind>(a.length).fill("del"), ...new Array<DiffKind>(b.length).fill("add")];
}

export function diffLines(before: string, after: string): DiffLine[] {
  const a = splitLines(before);
  const b = splitLines(after);

  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head++;
  let tail = 0;
  while (tail < a.length - head && tail < b.length - head && a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail++;

  const script: DiffKind[] = [
    ...new Array<DiffKind>(head).fill("same"),
    ...middleScript(a.slice(head, a.length - tail), b.slice(head, b.length - tail)),
    ...new Array<DiffKind>(tail).fill("same"),
  ];

  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  let k = 0;
  while (k < script.length) {
    if (script[k] === "same") {
      out.push({ kind: "same", text: a[i], oldNo: i + 1, newNo: j + 1 });
      i++;
      j++;
      k++;
      continue;
    }
    // A changed block: all its removed lines, then all its added lines.
    let dels = 0;
    let adds = 0;
    while (k < script.length && script[k] !== "same") {
      if (script[k] === "del") dels++;
      else adds++;
      k++;
    }
    for (let d = 0; d < dels; d++, i++) out.push({ kind: "del", text: a[i], oldNo: i + 1, newNo: null });
    for (let d = 0; d < adds; d++, j++) out.push({ kind: "add", text: b[j], oldNo: null, newNo: j + 1 });
  }
  return out;
}

export function diffStats(lines: DiffLine[]): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const line of lines) {
    if (line.kind === "add") added++;
    else if (line.kind === "del") removed++;
  }
  return { added, removed };
}
