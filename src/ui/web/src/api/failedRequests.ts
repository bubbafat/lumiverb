/**
 * Requests this browser couldn't make at all: no answer came (the connection dropped: Safari's "Load failed",
 * Chrome's "Failed to fetch"), so the server never saw them and nothing else would say so. Each is kept here,
 * its path without its query (a search's words stay in the browser), and sent once a request gets through
 * (POST /v1/me/failed-requests), for Admin › Logs. At most 20 are kept, in this tab only; a send the server
 * refuses (a demo's, a viewer's past the limit) is dropped, and one that can't be made is kept for the next.
 * A request the app called off itself (AbortError) isn't one.
 */

export interface FailedRequest {
  method: string;
  path: string;
  error: string;
  at: string;
  page: string;
}

const KEPT = 20;
const SHORT = 200;
const ROUTE = "/me/failed-requests";
// What the server takes is what Python prints (str.isprintable): letters, marks, numbers, punctuation, symbols
// and the plain space. Anything else (a control character, a line separator, a no-break space) is a space here.
const NOT_PRINTED = new RegExp("[^\\p{L}\\p{M}\\p{N}\\p{P}\\p{S} ]", "gu");

let pending: FailedRequest[] = [];
let sending = false;

/** One line of plain text, as the server takes it. */
function plain(text: string): string {
  return text.replace(NOT_PRINTED, " ").slice(0, SHORT);
}

/** A request to /v1{path} got no answer. */
export function noteFailed(method: string, path: string, err: unknown): void {
  if (path.startsWith(ROUTE) || (err instanceof DOMException && err.name === "AbortError")) return;
  pending = [
    ...pending,
    {
      method: method.toUpperCase(),
      path: plain(`/v1${path.split("?")[0]}`.replace(/\s/g, "%20")),
      error: plain(err instanceof Error ? err.message : String(err)) || "no answer",
      at: new Date().toISOString(),
      page: plain(window.location.pathname),
    },
  ].slice(-KEPT);
}

/** A request got through: what failed before goes to the server, with this token (none: kept). */
export function sendFailed(token: string): void {
  if (sending || pending.length === 0 || !token) return;
  const failed = pending;
  pending = [];
  sending = true;
  fetch(`/v1${ROUTE}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ failed }),
  })
    .catch(() => {
      pending = [...failed, ...pending].slice(-KEPT);
    })
    .finally(() => {
      sending = false;
    });
}
