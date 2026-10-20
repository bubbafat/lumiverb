// The names of Lumiverb's own data the web app says more of or asks for (ADR-024, docs/data-names.md): the
// server sends every producer and file by its name, and these are the ones a page looks for.
export const FACES = "lumiverb.image.faces";
export const VIDEO_FACES = "lumiverb.video.faces";
export const LOCATION_GUESS = "lumiverb.location.guess";
// A clip's files (GET /v1/assets/{id}/artifacts/{name}), and what plays (playback's source).
export const PREVIEW = "lumiverb.preview";
export const THUMBNAIL = "lumiverb.preview.thumbnail";
export const VIDEO_PREVIEW = "lumiverb.video.preview";
export const ANALYSIS_PROXY = "lumiverb.video.analysis_proxy";
