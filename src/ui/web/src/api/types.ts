export interface LibraryListItem {
  library_id: string;
  name: string;
  root_path: string;
  last_scan_at: string | null;
  status: string;
  is_public: boolean;
  /** In the trash since (status "trashed"); older servers leave it out. */
  trashed_at?: string | null;
  /** Bumped whenever the library's clips change; older servers leave it out. */
  revision?: number;
  /** Who reads its files: "server", this server, or a device's id (ADR-018). Older servers leave it out. */
  reader?: string;
  /** The device's name, for people; null for this server. */
  reader_name?: string | null;
  /** Its own Scans pause (Pause all isn't said here). Older servers leave it out. */
  scans_paused?: boolean;
  /** A scan of the whole library asked for (Scan now), until one started after it finishes. */
  scan_requested_at?: string | null;
  /** Its reader's latest check-in (ADR-018 phase 4); none before the first, and older servers leave it out. */
  checked_in?: ReaderCheckIn | null;
  /** Only the account's admins and the people given a role on it see it (ADR-018 decision 8). */
  private?: boolean;
  /** Its clips without a location are placed from those taken around the same time (ADR-017). */
  infer_location?: boolean;
  /** The caller's role on it: "admin" (its admin), "editor" or "viewer". Older servers leave it out. */
  role?: LibraryRole | null;
}

/** A plug-in's acknowledgment (ADR-021): what an admin reads and accepts before it's on (a license, say). */
export interface PluginAcknowledgment {
  version: string;
  title: string;
  /** Paragraphs, separated by a blank line. */
  text: string;
  links: { label: string; url: string }[];
}

/** An optional part of Lumiverb an account turns on: a program of its own installed on this server, Lumiverb's own
 * or the server admin's (ADR-023). */
export interface Plugin {
  plugin_id: string;
  title: string;
  description: string;
  /** waiting: turned on, but the plug-in says its acknowledgment must be accepted again (off until accepted). */
  state: "on" | "off" | "waiting";
  acknowledgment: PluginAcknowledgment | null;
  /** The acceptance it's on by (or waiting with); who, for admins. */
  acknowledged: { version: string; by: string | null; at: string } | null;
  /** Every plug-in is installed since ADR-023 phase 3. */
  kind?: "installed";
  version?: string | null;
  author?: string | null;
  homepage?: string | null;
  /** Its default, in every account whose admin hasn't turned it on or off; and whether this one hasn't. */
  on_by_default?: boolean | null;
  follows_default?: boolean;
  /** In words: what it reads ("each clip's location (…)") and what it answers. */
  reads?: string[];
  answers?: string[];
  /** The lookups it answers, by name: "places" is Set location's Place search. */
  lookups?: string[];
  downloads?: { url: string; title: string; about_mb: number }[];
  internet?: boolean;
  /** Its process, as the plug-in runner last said. */
  running?: PluginRunning | null;
  /** Its settings, from its manifest: the account's, and the server's for a server admin. */
  settings?: PluginSetting[];
  /** Saving settings: why the plug-in's own check didn't run (they were saved). */
  unchecked?: string | null;
}

/** An installed plug-in's setting: its schema, as its manifest declares it, and its value here (ADR-023). */
export interface PluginSetting {
  key: string;
  type: "int" | "float" | "bool" | "text" | "choice" | "secret";
  label: string;
  help: string;
  /** null for a secret, which is never shown again: only whether it's set. */
  default: number | boolean | string | null;
  value: number | boolean | string | null;
  /** Chosen here rather than its default (a secret: set at all). */
  set: boolean;
  /** False for a secret set that this install can't read (set on another install: a backup restored here). */
  readable?: boolean;
  minimum?: number | null;
  maximum?: number | null;
  unit?: string | null;
  pattern?: string | null;
  max_length?: number | null;
  choices: { value: string; label: string }[];
  /** account: each account's admin sets it; server: the server admin does. */
  scope: "account" | "server";
  advanced: boolean;
  remakes: boolean;
  /** Whether you may change it. */
  editable: boolean;
}

export interface PluginRunning {
  /** off: on in no account, so not run (started when an account turns it on). */
  state: "starting" | "ready" | "preparing" | "failing" | "restarting" | "stopped" | "off" | "not_running";
  why?: string | null;
  /** Preparing, or ready with work in the background: done of total, and what it says it's doing. */
  detail?: string | null;
  done?: number | null;
  total?: number | null;
  since?: string | null;
  memory_mb?: number | null;
  restarts?: number;
}

/** A place found by name (a plug-in that finds places): what setting a location by place takes. */
export interface Place {
  place_id: string;
  name: string;
  region: string | null;
  country: string | null;
  /** "Port of Spain, Trinidad and Tobago". */
  label: string;
  lat: number;
  lon: number;
  /** 0 for a landmark. */
  population: number;
  /** A landmark's kind ("amusement park"); null: a town. */
  kind?: string | null;
  /** The name in another language it was found by, when not its own ("Eiffel Tower" for Tour Eiffel). */
  matched?: string | null;
  /** Who found it: the plug-in's id, and its title. */
  source?: string;
  source_title?: string | null;
}

/** Shown beside place names: their source and license ask it. */
export interface PlaceCredit {
  text: string;
  url: string;
  license_url: string;
}

/** What a place search found, from each source, and who to credit; the plug-ins that couldn't answer now. */
export interface PlaceSearch {
  places: Place[];
  /** Always null since every place is a plug-in's: credits says who to credit. */
  credit: PlaceCredit | null;
  credits?: { source: string; text: string; url: string | null; license?: License | null }[];
  unavailable?: { plugin_id: string; title: string; why: string }[];
}

/** The license a source's data is under, linked beside its credit ("CC BY 4.0"). */
export interface License {
  text: string;
  url: string;
}

/** Who to credit for what a plug-in shows or finds, and its license when it names one. */
export interface Credit {
  text: string;
  url: string | null;
  license?: License | null;
}

/** A role on one library (ADR-018 decision 8): "none" means they don't see it. */
export type LibraryRole = "admin" | "editor" | "viewer" | "none";

/** Someone in the account, as a library's admin sees them: their role on it, and the role set for them there. */
export interface LibraryPerson {
  user_id: string;
  email: string;
  account_role: string;
  /** False: they see only the libraries they're given a role on. */
  every_library: boolean;
  role: LibraryRole;
  /** Set for them on this library; null when the account's rule gives it. */
  set_role: LibraryRole | null;
  set_by: string | null;
  set_at: string | null;
}

export interface LibraryPeople {
  library_id: string;
  private: boolean;
  people: LibraryPerson[];
}

export interface ReaderCheckIn {
  /** "this server", or a device's name. */
  client: string;
  version: string;
  /** "away": its storage can't be reached from the reader. */
  state: "scanning" | "idle" | "paused" | "away";
  waiting_files: number;
  waiting_bytes: number;
  last_full_scan_at: string | null;
  checked_in_at: string;
  /** Why it holds its remakes back now (a device's, ADR-018 decision 6): on battery, or a metered network. */
  remakes_held?: "battery" | "metered" | null;
}

export interface DirectoryNode {
  name: string;
  path: string;
  asset_count: number;
}

export interface LibraryResponse {
  library_id: string;
  name: string;
  root_path: string;
  is_public: boolean;
  /** Who reads its files: "server", this server, or a device's id (ADR-018). Older servers leave it out. */
  reader?: string;
  /** The device's name, for people; null for this server. */
  reader_name?: string | null;
  /** Only the account's admins and the people given a role on it see it. */
  private?: boolean;
  /** Its clips without a location are placed from those taken around the same time (ADR-017). */
  infer_location?: boolean;
  /** The caller's role on it. Older servers leave it out. */
  role?: LibraryRole | null;
}

export interface EmptyTrashResponse {
  deleted: number;
}

export interface AssetPageItem {
  asset_id: string;
  rel_path: string;
  file_size: number;
  file_mtime: string | null;
  content_hash: string | null;
  media_type: string;
  width: number | null;
  height: number | null;
  taken_at: string | null;
  status: string;
  duration_sec: number | null;
  camera_make: string | null;
  camera_model: string | null;
  iso: number | null;
  aperture: number | null;
  focal_length: number | null;
  focal_length_35mm: number | null;
  lens_model: string | null;
  flash_fired: boolean | null;
  gps_lat: number | null;
  gps_lon: number | null;
  face_count?: number | null;
  created_at: string | null;
}

export interface AssetPageResponse {
  items: AssetPageItem[];
  next_cursor: string | null;
}

export interface FacetsResponse {
  media_types: string[];
  camera_makes: string[];
  camera_models: string[];
  lens_models: string[];
  iso_range: (number | null)[];
  aperture_range: (number | null)[];
  focal_length_range: (number | null)[];
  /** Clips with a location: the file's or a person's (guesses too with include_guesses). */
  has_gps_count: number;
  has_face_count: number;
  /** Clips with an applied guess: the "Includes guesses" toggle shows only when there are some. */
  guess_count?: number;
}

export interface FaceItem {
  face_id: string;
  bounding_box: { x: number; y: number; w: number; h: number } | null;
  detection_confidence: number | null;
  person: { person_id: string; display_name: string; dismissed: boolean } | null;
  /** A video's face: the moment its box is from, and every moment the person was seen at (ms). */
  frame_ms?: number | null;
  seen_ms?: number[] | null;
}

export interface FaceListResponse {
  faces: FaceItem[];
}

export interface SearchHit {
  type: "image" | "scene" | "transcript";
  asset_id: string;
  library_id: string | null;
  library_name: string | null;
  rel_path: string;
  thumbnail_key: string | null;
  proxy_key: string | null;
  description: string;
  tags: string[];
  score: number;
  source: string;
  camera_make: string | null;
  camera_model: string | null;
  scene_id: string | null;
  start_ms: number | null;
  end_ms: number | null;
  media_type: string | null;
  file_size: number | null;
  duration_sec?: number | null;
  width: number | null;
  height: number | null;
  taken_at: string | null;
  snippet: string | null;
  language: string | null;
}

export interface SearchResponse {
  query: string;
  hits: SearchHit[];
  total: number;
  source: string;
}

export interface AssetDetail {
  asset_id: string;
  library_id: string;
  rel_path: string;
  media_type: string;
  status: string;
  proxy_key: string | null;
  thumbnail_key: string | null;
  video_preview_key: string | null;
  duration_sec: number | null;
  width: number | null;
  height: number | null;
  // Source-file size in bytes. Optional because the lightbox uses the
  // page item's file_size first and only falls back to detail when the
  // page item is a synthesized placeholder (e.g. cluster review face
  // drill-down).
  file_size?: number | null;
  content_hash: string | null;
  exif_extracted_at: string | null;
  camera_make: string | null;
  camera_model: string | null;
  taken_at: string | null;
  gps_lat: number | null;
  gps_lon: number | null;
  iso: number | null;
  exposure_time_us: number | null;
  aperture: number | null;
  focal_length: number | null;
  focal_length_35mm: number | null;
  lens_model: string | null;
  flash_fired: boolean | null;
  orientation: number | null;
  ai_description?: string | null;
  ai_tags: string[];
  ocr_text?: string | null;
  /** Which of "description", "tags", "ocr_text" a person wrote (the one copy); the values above are what they see. */
  corrected?: string[];
  transcript_srt?: string | null;
  transcript_language?: string | null;
  /** Whose transcript is shown: "manual" (a person's) or the machine that made it. */
  transcript_source?: string | null;
  transcribed_at?: string | null;
  note?: string | null;
  note_author?: string | null;
  note_updated_at?: string | null;
  gps_accuracy_m?: number | null;
  /** Faces found in it (null: not looked for yet, or a public page's); a video's, the people seen. */
  face_count?: number | null;
  /** What the file doesn't say (ADR-017), beside the file's GPS. Signed-in only. */
  location?: AssetLocation | null;
  /** What installed plug-ins' work says of it (ADR-023): each plug-in on here that made it a fact. Signed-in only. */
  facts?: PluginFacts[] | null;
}

/** A plug-in the server admin uninstalled with what it made kept: its facts shown and searched as they were. */
export interface KeptPlugin {
  plugin_id: string;
  title: string;
  version: string;
  removed_at: string;
  /** The clips in this account it made facts for. */
  clips: number;
}

/** One plug-in's facts about a clip, in its manifest's order, and who to credit for them. */
export interface PluginFacts {
  plugin_id: string;
  title: string;
  credit: Credit | null;
  /** Uninstalled, with what it made kept by the server admin: shown as it was, made no more. */
  removed?: boolean;
  /** empty: it had nothing to say, and text is the line its manifest declares for that ("Not near a town"). */
  facts: { key: string; label: string; text: string; url: string | null; empty?: boolean }[];
}

/** A clip's location beyond the file's: a person's, a guess, or a suggestion. */
export interface AssetLocation {
  lat: number;
  lon: number;
  /** 0 for a person's exact point. */
  radius_m: number;
  source: "person" | "time" | "suggestion";
  status: "applied" | "suggested";
  /** Why, in a few words ("Same place as IMG_1.jpg", "from phone photos 14:02–14:40"). */
  basis_summary?: string | null;
  /** A guess whose fixes went, with nothing else to go by: kept, less sure ("source removed"). */
  basis_gone?: boolean;
  /** A guess whose fixes a narrower window left outside, with nothing else to go by: kept, less sure. */
  outside_window?: boolean;
  /** Who set it, for a person's: looked up when a signed-in person reads it (null for an API key). */
  set_by?: string | null;
  set_at?: string | null;
  /** Who a place name in basis_summary is from, to show beside it with its license; null without one. */
  credit?: Credit | null;
}

export interface SimilarHit {
  asset_id: string;
  rel_path: string;
  thumbnail_key: string | null;
  proxy_key: string | null;
  distance: number;
  media_type: string | null;
  file_size: number | null;
  width: number | null;
  height: number | null;
}

export interface UserItem {
  user_id: string;
  email: string;
  role: string;
  /** False: they see only the libraries they're given a role on (ADR-018 decision 8). Older servers leave it out. */
  every_library?: boolean;
  created_at: string;
  last_login_at: string | null;
  /** Made just now by an invite: the link to send them (it works once, for 7 days), at each
   * address other people open Lumiverb at, the likeliest to open first. */
  invite_urls?: string[] | null;
  invite_expires_at?: string | null;
}

/** A new invite link for someone who hasn't signed in yet. */
export interface InviteLink {
  invite_urls: string[];
  invite_expires_at: string;
}

export interface SimilarityResponse {
  source_asset_id: string;
  hits: SimilarHit[];
  total: number;
  embedding_available: boolean;
}

export interface CurrentUser {
  user_id: string | null;
  email: string | null;
  role: string;
  /** The install's admin too: where previews live, the network, backups, logs (only when signed in). */
  server_admin: boolean;
  /** Trying it as a viewer (a demo server's): a session that reads only, so nothing that changes anything is offered. */
  demo?: boolean;
}

/** One of the install's accounts, as its server admins see them (Admin › Accounts). */
export interface AccountItem {
  tenant_id: string;
  name: string;
  plan: string;
  /** "active", or "deleted": kept, never reached again. */
  status: string;
  /** The people who sign in to it. */
  users: number;
  created_at: string | null;
}

/** An account just made with its first admin invited: the link to send them, shown once. */
export interface AccountMade {
  tenant_id: string;
  invite_urls: string[] | null;
  invite_expires_at: string | null;
}

/** Someone who's a server admin, and their account. */
export interface ServerAdminItem {
  user_id: string;
  email: string;
  tenant_id: string;
  account: string;
}

export interface ApiKeyItem {
  key_id: string;
  label: string | null;
  role: string;
  last_used_at: string | null;
  created_at: string;
  /** When it ends unless used by then (the account's key-idle days); null when they're off, or for Lumiverb's own keys. */
  ends_at?: string | null;
}

export interface ApiKeyCreateResponse extends ApiKeyItem {
  plaintext: string;
}

export interface LibraryRevision {
  library_id: string;
  revision: number;
  asset_count: number;
}

export interface LibraryHealthItem {
  library_id: string;
  healthy: boolean;
  pending: number;
}

/** Legacy saved query format (deprecated). */
export interface SavedQueryLegacy {
  q?: string;
  filters: Record<string, unknown>;
  library_id?: string;
}

/** New saved query format using filter algebra. */
export interface SavedQuery {
  filters: Array<{ type: string; value: string }>;
  sort?: string;
  direction?: string;
}

export interface ProjectItem {
  project_id: string;
  name: string;
  description: string | null;
  cover_asset_id: string | null;
  owner_user_id: string | null;
  visibility: string;  // "private" | "shared" | "public"
  ownership: string;   // "own" | "shared"
  sort_order: string;
  asset_count: number;
  created_at: string;
  updated_at: string;
  status?: "active" | "archived";
  archived_at?: string | null;
  /** Set while the project is in the trash. */
  deleted_at?: string | null;
  /** Clips someone trashed: hidden and not exported until restored. */
  trashed_asset_count?: number;
  /** Clips whose files went missing: back when the files are. */
  missing_asset_count?: number;
  /** Clips whose library is in the trash: back if the library is restored. */
  library_trashed_asset_count?: number;
  /** Clips a person archived: hidden and not exported until unarchived. */
  archived_asset_count?: number;
}

export interface ProjectListResponse {
  items: ProjectItem[];
}

export interface ProjectAssetItem {
  asset_id: string;
  rel_path: string;
  file_size: number;
  media_type: string;
  width: number | null;
  height: number | null;
  taken_at: string | null;
  status: string;
  duration_sec: number | null;
  camera_make: string | null;
  camera_model: string | null;
}

export interface ProjectAssetsResponse {
  items: ProjectAssetItem[];
  next_cursor: string | null;
}

export interface BatchAddResponse {
  added: number;
}

export interface BatchRemoveResponse {
  removed: number;
}

// ---------------------------------------------------------------------------
// Unified Browse (ADR-008)
// ---------------------------------------------------------------------------

export interface BrowseItem extends AssetPageItem {
  library_id: string;
  library_name: string;
}

export interface BrowseResponse {
  items: BrowseItem[];
  next_cursor: string | null;
}

// ---------------------------------------------------------------------------
// Ratings (ADR-007)
// ---------------------------------------------------------------------------

export type RatingColor = "red" | "orange" | "yellow" | "green" | "blue" | "purple";

export const RATING_COLORS: RatingColor[] = ["red", "orange", "yellow", "green", "blue", "purple"];

export const COLOR_HEX: Record<RatingColor, string> = {
  red: "#ef4444",
  orange: "#f97316",
  yellow: "#eab308",
  green: "#22c55e",
  blue: "#3b82f6",
  purple: "#a855f7",
};

export interface AssetRating {
  favorite: boolean;
  stars: number;
  color: RatingColor | null;
}

export interface RatingResponse {
  asset_id: string;
  favorite: boolean;
  stars: number;
  color: RatingColor | null;
}

export interface RatingLookupResponse {
  ratings: Record<string, AssetRating>;
}

export interface BatchRatingResponse {
  updated: number;
}

