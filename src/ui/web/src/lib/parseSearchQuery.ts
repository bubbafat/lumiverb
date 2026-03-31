/**
 * Parse structured filters out of a search query string.
 *
 * Extracts tokens like `is:favorite`, `star:3`, `star:>=4`, `color:red`
 * and returns the remaining text query plus extracted filter params.
 */
export interface ParsedQuery {
  text: string;
  filters: Record<string, string>;
}

const FILTER_PATTERNS: { regex: RegExp; handler: (match: RegExpMatchArray) => Record<string, string> }[] = [
  {
    regex: /\bis:favorite\b/i,
    handler: () => ({ favorite: "true" }),
  },
  {
    regex: /\bis:rated\b/i,
    handler: () => ({ has_rating: "true" }),
  },
  {
    regex: /\bis:unrated\b/i,
    handler: () => ({ has_rating: "false" }),
  },
  {
    regex: /\bstar:(\d)\b/,
    handler: (m) => ({ star_min: m[1], star_max: m[1] }),
  },
  {
    regex: /\bstar:>=(\d)\b/,
    handler: (m) => ({ star_min: m[1] }),
  },
  {
    regex: /\bstar:>(\d)\b/,
    handler: (m) => ({ star_min: String(Number(m[1]) + 1) }),
  },
  {
    regex: /\bstar:<=(\d)\b/,
    handler: (m) => ({ star_max: m[1] }),
  },
  {
    regex: /\bstar:<(\d)\b/,
    handler: (m) => ({ star_max: String(Number(m[1]) - 1) }),
  },
  {
    regex: /\bstar:(none|0)\b/i,
    handler: () => ({ star_max: "0" }),
  },
  {
    regex: /\bhas:star\b/i,
    handler: () => ({ star_min: "1" }),
  },
  {
    regex: /\bhas:color\b/i,
    handler: () => ({ has_color: "true" }),
  },
  {
    regex: /\bcolor:(red|orange|yellow|green|blue|purple)\b/i,
    handler: (m) => ({ color: m[1].toLowerCase() }),
  },
  {
    regex: /\bcolor:none\b/i,
    handler: () => ({ color: "none" }),
  },
  {
    regex: /\bhas:faces\b/i,
    handler: () => ({ has_faces: "true" }),
  },
  {
    regex: /\bperson:"([^"]+)"/i,
    handler: (m) => ({ person: m[1] }),
  },
];

export function parseSearchQuery(query: string): ParsedQuery {
  let text = query;
  const filters: Record<string, string> = {};

  for (const { regex, handler } of FILTER_PATTERNS) {
    const match = text.match(regex);
    if (match) {
      Object.assign(filters, handler(match));
      text = text.replace(regex, "");
    }
  }

  text = text.replace(/\s+/g, " ").trim();
  return { text, filters };
}
