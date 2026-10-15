/** Where sign-in goes after it succeeds: the `next` it was sent with when that is a path on this site, else "/".
 * Only the path as the browser parses it is used (path, query and fragment), never the text as given, and a path
 * that would start with "//" keeps one slash: a link or navigate() reads "//name" as another site. */
export function signInNext(next: string | null, origin: string = window.location.origin): string {
  if (!next) return "/";
  let url: URL;
  try {
    url = new URL(next, origin);
  } catch {
    return "/";
  }
  if (url.origin !== origin) return "/";
  return url.pathname.replace(/^\/+/, "/") + url.search + url.hash;
}
