import type { SearchContext } from "../api/types";
import { clock } from "./faceMoments";

/** What found a clip a search found, said for a person (GET /v1/query's search_context). */
export interface FoundBy {
  /** "Text in photo", "Said at 1:23", "Near Dundee"… */
  label: string;
  /** The words that matched (an excerpt), when there are some to show. */
  detail: string | null;
  /** Where in a video (seconds): a scene's, or where it's said. */
  startAt: number | null;
  /** Worth flagging on its thumbnail: found by something other than its description. */
  flagged: boolean;
}

/** What found it, or null when the search doesn't say (a browse, or a search's words split between fields). */
export function foundBy(ctx: SearchContext | null | undefined, isVideo: boolean): FoundBy | null {
  if (!ctx?.matched_in) return null;
  const at = ctx.start_ms ?? null;
  const detail = ctx.snippet || null;
  const flag = (label: string, words: string | null = detail): FoundBy =>
    ({ label, detail: words, startAt: at != null ? at / 1000 : null, flagged: true });
  switch (ctx.matched_in) {
    case "description":
      return { ...flag("Description"), flagged: false };
    case "note":
      return flag("Note");
    case "text":
      return flag(isVideo ? "Text in video" : "Text in photo");
    case "transcript":
      return flag(at != null ? `Said at ${clock(at)}` : "Said in it");
    case "path":
      return flag("Folder or file name", null);
    case "fact":
      return flag(ctx.from_plugin || "A plug-in's fact");
    case "scene":
      return flag(at != null ? `Scene at ${clock(at)}` : "A scene");
    case "place":
      return flag(detail ? `Near ${detail}` : "Near the place", null);
    case "person":
      return flag(detail ? `Recognised: ${detail}` : "Recognised", null);
    default:
      return null;
  }
}

/** One line: its label and the words that matched. */
export function foundByLine(found: FoundBy): string {
  return found.detail ? `${found.label}: ${found.detail}` : found.label;
}
