/**
 * API paths built from values: an id from a link (react-router decodes route
 * params, so it's whatever the link said) or from the data is one path
 * segment of its own, encoded, so a "/", "?", "#" or "%" in it stays in it and
 * a request never reaches another API path with the person's token.
 */

/** A value as one path segment. "." and ".." can't be one (a browser reads them, encoded or not, as a step
 * along the path), nor "": they're refused, as no id is either. */
export function segment(value: string | number): string {
  const text = String(value);
  if (text === "" || text === "." || text === "..") {
    throw new Error(`Not an id: "${text}"`);
  }
  return encodeURIComponent(text);
}

/** An API path whose every interpolated value is a segment (`segment`): apiPath`/libraries/${id}/people`.
 * A query string goes after it, built with URLSearchParams. */
export function apiPath(parts: TemplateStringsArray, ...values: (string | number)[]): string {
  return parts.reduce((path, part, i) => path + part + (i < values.length ? segment(values[i]) : ""), "");
}
