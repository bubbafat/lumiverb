/** What a search was understood to ask for (GET /v1/query's `understood`, src/server/search/understand.py):
 * a person, a place, a kind of clip, and the words left. Shown under the search box; dismissing one
 * searches its words as words: quoted text is never understood. */

export interface UnderstoodPart {
  kind: "person" | "place" | "kind" | "also" | "words";
  /** What was typed for it, as the server read it (lowercase, accents aside). */
  words: string;
  /** What it was taken for: "Susan Horvick", "California Adventure", "videos"; for "also" (an AI-assisted search's
   * word naming a kind of thing), the things of that kind it's searched with too. */
  label: string;
}

const WORD = /[\p{L}\p{N}][\p{L}\p{N}-]*/gu;

function fold(word: string): string {
  return word.normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase();
}

/** The search with these words (as the server read them) in quotes, as typed, so they're searched as
 * words: the first time they're typed in a row outside quotes. The search as it was when they aren't. */
export function takenLiterally(query: string, words: string): string {
  const want = words.split(" ").filter(Boolean);
  if (want.length === 0) return query;
  // The words typed outside quotes, with where each is.
  const typed: { word: string; start: number; end: number }[] = [];
  let quoted = false;
  let last = 0;
  const outside = (from: number, to: number) => {
    for (const m of query.slice(from, to).matchAll(WORD)) {
      const start = from + (m.index ?? 0);
      typed.push({ word: fold(m[0]), start, end: start + m[0].length });
    }
  };
  for (let i = 0; i < query.length; i++) {
    if (query[i] !== '"') continue;
    if (!quoted && query.indexOf('"', i + 1) === -1) break; // an unmatched quote: the rest is words
    if (!quoted) outside(last, i);
    quoted = !quoted;
    last = i + 1;
  }
  if (!quoted) outside(last, query.length);
  for (let i = 0; i + want.length <= typed.length; i++) {
    if (want.every((w, k) => typed[i + k].word === w)) {
      const start = typed[i].start;
      const end = typed[i + want.length - 1].end;
      return `${query.slice(0, start)}"${query.slice(start, end)}"${query.slice(end)}`;
    }
  }
  return query;
}
