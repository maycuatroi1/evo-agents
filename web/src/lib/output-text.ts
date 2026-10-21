/**
 * How the viewer reads a text output (components/data/output-text.tsx), apart from lib/outputs.ts so that a page that
 * only lists outputs carries none of it.
 */

/** The most bytes of a text file the viewer reads; a longer one shows its start and offers the download. */
export const TEXT_PREVIEW_BYTES = 1024 * 1024;
/** Rows of a CSV the viewer draws as a table. */
export const CSV_ROWS = 500;

/** JSON indented by two spaces; JSON Lines one value a line, each indented; the text as it is when it does not parse. */
export function prettyJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    const lines = text.split("\n").filter((line) => line.trim() !== "");
    if (lines.length < 2) return text;
    try {
      return lines.map((line) => JSON.stringify(JSON.parse(line), null, 2)).join("\n");
    } catch {
      return text;
    }
  }
}

/**
 * The rows of a CSV (or, with a tab, TSV) text as RFC 4180 reads them: fields split by the delimiter, a quoted field
 * may hold the delimiter, line breaks and doubled quotes. At most `limit` rows; `more` says whether the text has others.
 */
export function parseCsv(text: string, delimiter = ",", limit = CSV_ROWS): { rows: string[][]; more: boolean } {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  let index = 0;
  const endRow = () => {
    row.push(field);
    field = "";
    if (row.length > 1 || row[0] !== "") rows.push(row);
    row = [];
  };
  while (index < text.length) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') {
        field += '"';
        index += 1;
      } else if (char === '"') quoted = false;
      else field += char;
    } else if (char === '"' && field === "") quoted = true;
    else if (char === delimiter) {
      row.push(field);
      field = "";
    } else if (char === "\n" || char === "\r") {
      if (char === "\r" && text[index + 1] === "\n") index += 1;
      endRow();
      if (rows.length > limit) return { rows: rows.slice(0, limit), more: true };
    } else field += char;
    index += 1;
  }
  if (field !== "" || row.length) endRow();
  return { rows: rows.slice(0, limit), more: rows.length > limit };
}
