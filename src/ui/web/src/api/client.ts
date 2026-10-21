import type {
  AccountItem,
  AccountMade,
  KeptPlugin,
  ServerAdminItem,
  PlaceSearch,
  Plugin,
  ApiKeyCreateResponse,
  ApiKeyItem,
  AssetDetail,
  BatchAddResponse,
  BatchRemoveResponse,
  ProjectAssetsResponse,
  ProjectItem,
  ProjectListResponse,
  CurrentUser,
  DirectoryNode,
  EmptyTrashResponse,
  FaceListResponse,
  FacetsResponse,
  InviteLink,
  LibraryHealthItem,
  LibraryListItem,
  LibraryPeople,
  LibraryPerson,
  LibraryResponse,
  LibraryRevision,
  LibraryRole,
  ReaderCheckIn,
  SimilarityResponse,
  UserItem,
  RatingResponse,
  RatingLookupResponse,
  BatchRatingResponse,
} from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    /** The envelope's error code, e.g. "in_projects" for a 409 that needs the user's say. */
    public code?: string,
    /** The facts behind the code, e.g. which projects would lose clips. */
    public details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export const API_KEY_STORAGE_KEY = "lumiverb_api_key";
const LEGACY_AUTH_KEYS = [
  "lumiverb_token",
  "lumiverb_jwt",
  "lumiverb_access_token",
  "lumiverb_auth_disabled",
];

function getStoredApiKey(): string {
  try {
    return localStorage.getItem(API_KEY_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setApiKey(token: string): void {
  try {
    localStorage.setItem(API_KEY_STORAGE_KEY, token);
  } catch {
    // ignore
  }
}

export function clearApiKey(): void {
  try {
    localStorage.removeItem(API_KEY_STORAGE_KEY);
    for (const key of LEGACY_AUTH_KEYS) {
      localStorage.removeItem(key);
    }
  } catch {
    // ignore
  }
}

export function getApiKey(): string {
  return getStoredApiKey();
}

const authHeaders = (): Record<string, string> => {
  const key = getApiKey();
  return key ? { Authorization: `Bearer ${key}` } : {};
};

let _refreshing: Promise<boolean> | null = null;

/**
 * Attempt to refresh the JWT. Returns true if a new token was obtained.
 * Coalesces concurrent refresh attempts into a single request.
 */
async function tryRefresh(): Promise<boolean> {
  if (_refreshing) return _refreshing;
  _refreshing = (async () => {
    try {
      const res = await fetch("/v1/auth/refresh", {
        method: "POST",
        headers: authHeaders(),
      });
      if (res.ok) {
        const data = (await res.json()) as { access_token: string };
        setApiKey(data.access_token);
        return true;
      }
    } catch {
      // Refresh failed — will fall through to logout
    }
    return false;
  })();
  try {
    return await _refreshing;
  } finally {
    _refreshing = null;
  }
}

function handleUnauthorized(): void {
  const stored = getStoredApiKey();
  if (!stored) return;

  clearApiKey();
  window.location.href = "/login";
}

export async function logout(): Promise<void> {
  try {
    await fetch("/v1/auth/logout", { method: "POST", headers: authHeaders() });
  } catch {
    // Best-effort — server may be unreachable.
  }
  clearApiKey();
  window.location.href = "/login";
}

type AuthFetchInit = Omit<RequestInit, "headers"> & { headers?: Record<string, string> };

/**
 * fetch `/v1${path}` with the stored token. On a 401 it refreshes the token
 * once and retries, and hands back the retry's response whatever it is, so a
 * 409 decision or a 404 after a refresh reaches the caller like any other.
 * It signs out only when the refresh fails or the retry is 401 again. A
 * refresh that fails after another tab stored a new token retries with that
 * token instead of signing every tab out.
 */
export async function authFetch(path: string, init: AuthFetchInit = {}): Promise<Response> {
  const send = (token: string) =>
    fetch(`/v1${path}`, {
      ...init,
      headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...init.headers },
    });
  const used = getApiKey();
  let res = await send(used);
  if (res.status !== 401) return res;
  // No stored token: nothing to refresh, and nobody to sign out.
  if (!getApiKey()) return res;
  const refreshed = await tryRefresh();
  const stored = getApiKey();
  if (refreshed || (stored && stored !== used)) {
    res = await send(stored);
    if (res.status !== 401) return res;
  }
  handleUnauthorized();
  return res;
}

/** Turn a non-OK response into an ApiError, reading the error envelope. */
async function toApiError(res: Response): Promise<ApiError> {
  let message = res.statusText;
  let code: string | undefined;
  let details: Record<string, unknown> | undefined;
  try {
    const json = (await res.json()) as {
      error?: { code?: string; message?: string; details?: Record<string, unknown> };
    };
    message = json?.error?.message ?? message;
    code = json?.error?.code;
    details = json?.error?.details;
  } catch {
    // not JSON
  }
  return new ApiError(res.status, message, code, details);
}

type ApiFetchOptions = Omit<AuthFetchInit, "body"> & { body?: unknown };

async function apiFetch<T>(
  path: string,
  options?: ApiFetchOptions,
): Promise<T> {
  const { body, headers, ...rest } = options ?? {};
  const res = await authFetch(path, {
    ...rest,
    headers: { "Content-Type": "application/json", ...headers },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw await toApiError(res);
  if (res.status === 204) {
    return null as T;
  }
  return res.json() as Promise<T>;
}

export async function listLibraries(
  includeTrash?: boolean,
): Promise<LibraryListItem[]> {
  const qs = includeTrash ? "?include_trashed=true" : "";
  return apiFetch<LibraryListItem[]>(`/libraries${qs}`);
}

export async function createLibrary(
  name: string,
  rootPath: string,
  reader?: string,
  inferLocation = false,
): Promise<LibraryResponse> {
  return apiFetch<LibraryResponse>("/libraries", {
    method: "POST",
    body: {
      name,
      root_path: rootPath,
      ...(reader && reader !== "server" ? { reader } : {}),
      ...(inferLocation ? { infer_location: true } : {}),
    },
  });
}

/** Who reads a library's files (admins): "server", this server, or a registered device's id, never both at once.
 * rootPath: the folder as the new reader sees it (left out, it stays). */
export async function chooseLibraryReader(libraryId: string, reader: string, rootPath?: string): Promise<LibraryResponse> {
  return apiFetch<LibraryResponse>(`/libraries/${libraryId}/reader`, {
    method: "PUT",
    body: { reader, ...(rootPath !== undefined ? { root_path: rootPath } : {}) },
  });
}

/** A computer that reads libraries for this server (ADR-018 decision 8): account admins register them. */
export interface Device {
  device_id: string;
  name: string;
  registered_at: string;
  /** Whether it has a key: none once ended, or once the person who made it lost their access. */
  has_key: boolean;
  key_last_used_at: string | null;
  /** The ids of the libraries it reads. */
  libraries: string[];
}

/** A device with its key, shown this once. */
export interface DeviceWithKey extends Device {
  key: string;
}

export async function listDevices(): Promise<Device[]> {
  return (await apiFetch<{ devices: Device[] }>("/devices")).devices;
}

export async function registerDevice(name: string): Promise<DeviceWithKey> {
  return apiFetch<DeviceWithKey>("/devices", { method: "POST", body: { name } });
}

export async function renameDevice(deviceId: string, name: string): Promise<Device> {
  return apiFetch<Device>(`/devices/${deviceId}`, { method: "PATCH", body: { name } });
}

/** A new key, shown this once: the old one stops at once. */
export async function newDeviceKey(deviceId: string): Promise<DeviceWithKey> {
  return apiFetch<DeviceWithKey>(`/devices/${deviceId}/key`, { method: "POST" });
}

export async function endDeviceKey(deviceId: string): Promise<void> {
  return apiFetch<void>(`/devices/${deviceId}/key`, { method: "DELETE" });
}

/** 409 device_reads_libraries (details.libraries) while a library names it as its reader. */
export async function removeDevice(deviceId: string): Promise<void> {
  return apiFetch<void>(`/devices/${deviceId}`, { method: "DELETE" });
}

/**
 * Move a library to the trash with everything in it; it's deleted for good
 * after the trash days. When its clips are in projects the API answers 409
 * in_projects (with the projects) until `removeFromProjects` says the user
 * agreed: hidden there now, and gone from them once deleted for good.
 */
export async function deleteLibrary(libraryId: string, removeFromProjects = false): Promise<void> {
  return apiFetch<void>(`/libraries/${libraryId}`, {
    method: "DELETE",
    body: { remove_from_projects: removeFromProjects },
  });
}

/** Take a library out of the trash with the clips that went with it. It comes back private. */
export async function restoreLibrary(libraryId: string): Promise<LibraryResponse> {
  return apiFetch<LibraryResponse>(`/libraries/${libraryId}/restore`, { method: "POST" });
}

export async function getLibrary(libraryId: string): Promise<LibraryResponse> {
  return apiFetch<LibraryResponse>(`/libraries/${libraryId}`);
}

/** Who can see and edit a library: everyone in the account with their role on it (its admin's). */
export async function getLibraryPeople(libraryId: string): Promise<LibraryPeople> {
  return apiFetch<LibraryPeople>(`/libraries/${libraryId}/people`);
}

/** Set someone's role on a library (its admin's); null: the account's rule again. */
export async function setLibraryRole(libraryId: string, userId: string, role: LibraryRole | null): Promise<LibraryPerson> {
  return apiFetch<LibraryPerson>(`/libraries/${libraryId}/people/${encodeURIComponent(userId)}`, {
    method: "PUT",
    body: { role },
  });
}

/** Private: only the account's admins and the people given a role on it see it (its admin's). */
export async function setLibraryPrivate(libraryId: string, isPrivate: boolean): Promise<LibraryResponse> {
  return apiFetch<LibraryResponse>(`/libraries/${libraryId}`, { method: "PATCH", body: { private: isPrivate } });
}

/** Every plug-in and its state in this account (ADR-021). */
export async function listPlugins(): Promise<Plugin[]> {
  return (await apiFetch<{ plugins: Plugin[] }>("/plugins")).plugins;
}

/** The plug-ins uninstalled with what they made kept that made facts in this account. */
export async function listKeptPlugins(): Promise<KeptPlugin[]> {
  return (await apiFetch<{ plugins: KeptPlugin[] }>("/plugins/kept")).plugins;
}

/** A kept plug-in's facts deleted from this account (account admins): 409 confirm_delete_facts (details: count,
 * title) until count is how many clips it made facts for here. */
export async function deleteKeptFacts(pluginId: string, count: number): Promise<{ deleted: number }> {
  return apiFetch<{ deleted: number }>(`/plugins/${encodeURIComponent(pluginId)}/facts?count=${count}`,
                                       { method: "DELETE" });
}

/** Turn a plug-in on or off (account admins). Turning on one that asks an acknowledgment: 409
 * acknowledgment_required (details: version, title, text, links) unless acknowledged is that version. */
export async function setPlugin(pluginId: string, enabled: boolean, acknowledged?: string): Promise<Plugin> {
  return apiFetch<Plugin>(`/plugins/${pluginId}`, {
    method: "PUT",
    body: { enabled, ...(acknowledged !== undefined ? { acknowledged } : {}) },
  });
}

/** An installed plug-in's settings (ADR-023): each one named, a value or null for its default (a secret: cleared).
 * scope "account" is this account's (its admins'), "server" the whole server's (server admins'). 422
 * invalid_settings (details.errors: by key) for any its schema refuses, or the plug-in does. A change of a setting
 * that remakes (an account's): 409 redo_on_change (details.clips; question "remake") until redo says: true remakes
 * what the old settings made too, false is new ones only. */
export async function setPluginSettings(
  pluginId: string,
  settings: Record<string, number | boolean | string | null>,
  scope: "account" | "server",
  redo?: boolean,
): Promise<Plugin> {
  return apiFetch<Plugin>(`/plugins/${pluginId}/${scope === "server" ? "server-settings" : "settings"}`, {
    method: "PUT",
    body: { settings, ...(redo !== undefined ? { redo } : {}) },
  });
}

/** Whether a library's clips' locations are guessed (ADR-017; its admin's). Turning it off removes its guesses:
 * 409 guesses_exist (details.clips) until removeGuesses says so. */
export async function setLibraryInferLocation(
  libraryId: string,
  on: boolean,
  removeGuesses = false,
): Promise<LibraryResponse> {
  return apiFetch<LibraryResponse>(`/libraries/${libraryId}`, {
    method: "PATCH",
    body: { infer_location: on, ...(removeGuesses ? { remove_guesses: true } : {}) },
  });
}

export async function updateLibraryVisibility(
  libraryId: string,
  is_public: boolean,
): Promise<LibraryResponse> {
  return apiFetch<LibraryResponse>(`/libraries/${libraryId}`, {
    method: "PATCH",
    body: { is_public },
  });
}

/** Delete these trashed libraries for good. If their clips are in projects,
 * the server refuses (409 in_projects, with the projects) unless
 * removeFromProjects says the user agreed. */
export async function emptyTrash(removeFromProjects: boolean, libraryIds: string[]): Promise<EmptyTrashResponse> {
  return apiFetch<EmptyTrashResponse>("/libraries/empty-trash", {
    method: "POST",
    body: { remove_from_projects: removeFromProjects, library_ids: libraryIds },
  });
}

// ---------------------------------------------------------------------------
// Archive and trash for clips (Robert's model, Oct 8). Archive: out of sight,
// kept forever. Trash: restorable until it's deleted for good after the
// trash days. A clip whose file went missing is archived, and comes back by
// itself when the file does.
// ---------------------------------------------------------------------------

/** Clips by id, or every clip under a folder of a library ("" = the whole library). */
export type PickClips = { asset_ids: string[] } | { library_id: string; path: string };

/** Archive clips in sight. Those not in sight (already archived, in the trash) come back as skipped. */
export async function archiveClips(pick: PickClips): Promise<{ archived: string[]; skipped: string[] }> {
  return apiFetch("/assets/archive", { method: "POST", body: pick });
}

/** Bring back clips a person archived. A missing file's clip is skipped: it returns with its file. */
export async function unarchiveClips(pick: PickClips): Promise<{ unarchived: string[]; skipped: string[] }> {
  return apiFetch("/assets/unarchive", { method: "POST", body: pick });
}

/** Move clips (in sight or archived) to the trash. When projects use them the
 * API answers 409 in_projects until removeFromProjects says the user agreed. */
export async function trashClips(
  assetIds: string[],
  removeFromProjects = false,
): Promise<{ trashed: string[]; not_found: string[] }> {
  return apiFetch("/assets", {
    method: "DELETE",
    body: { asset_ids: assetIds, reason: "user", remove_from_projects: removeFromProjects },
  });
}

/** Take clips out of the trash, back to where they were: in sight, or the
 * archive for those archived before (to_archive). Anything else is skipped. */
export async function restoreClips(
  assetIds: string[],
): Promise<{ restored: string[]; skipped: string[]; to_archive?: string[] }> {
  return apiFetch("/assets/restore", { method: "POST", body: { asset_ids: assetIds } });
}

/** Delete for good the clips whose files went missing (admins), in a library
 * or under a folder. Asks first: 409 confirm_delete_missing (details.count,
 * details.listed_at) until `told` says both; then 409 in_projects unless removeFromProjects. */
export async function deleteMissingClips(
  scope: { libraryId: string; path?: string } | "all",
  told?: { count: number; listedAt: string },
  removeFromProjects = false,
): Promise<{ deleted: number }> {
  return apiFetch("/archive/missing", {
    method: "DELETE",
    body: {
      ...(scope === "all" ? { all: true } : { library_id: scope.libraryId }),
      ...(scope !== "all" && scope.path ? { path: scope.path } : {}),
      // What the admin was told: only clips missing since before then go.
      ...(told ? { count: told.count, missing_before: told.listedAt } : {}),
      remove_from_projects: removeFromProjects,
    },
  });
}

/** Delete clips in the trash for good now: these, or "all" shown (a
 * library's, under a folder, when given). Admins. 409 in_projects until
 * removeFromProjects. Never reaches archived clips or a trashed library's. */
export async function emptyClipTrash(
  assetIds: string[] | "all",
  removeFromProjects = false,
  scope: { libraryId?: string; path?: string; trashedBefore?: string } = {},
): Promise<{ deleted: number }> {
  return apiFetch("/trash/empty", {
    method: "DELETE",
    body: {
      ...(assetIds === "all" ? { all: true } : { asset_ids: assetIds }),
      ...(scope.libraryId ? { library_id: scope.libraryId } : {}),
      ...(scope.path ? { path: scope.path } : {}),
      // Only what was there when the view listed it: nothing trashed since.
      ...(scope.trashedBefore ? { trashed_before: scope.trashedBefore } : {}),
      remove_from_projects: removeFromProjects,
    },
  });
}

export interface HiddenClip {
  asset_id: string;
  library_id: string;
  library_name: string;
  rel_path: string;
  media_type: string;
}

export interface TrashedClip extends HiddenClip {
  trashed_at: string;
  /** When it's deleted for good; null when the trash is emptied by hand only. */
  expires_at: string | null;
}

export interface ArchivedClip extends HiddenClip {
  archived_at: string;
  /** Archived because the file went missing: it comes back with the file, not with Unarchive. */
  file_missing: boolean;
}

export interface HiddenPage<T> {
  items: T[];
  next_cursor: string | null;
  total: number;
}

export interface HiddenFilter {
  libraryId?: string;
  path?: string;
  after?: string;
  limit?: number;
}

function hiddenQuery(f: HiddenFilter, extra: Record<string, string> = {}): string {
  const qs = new URLSearchParams(extra);
  if (f.libraryId) qs.set("library_id", f.libraryId);
  if (f.path) qs.set("path", f.path);
  if (f.after) qs.set("after", f.after);
  if (f.limit) qs.set("limit", String(f.limit));
  return qs.toString();
}

/** Clips in the trash, most recently trashed first. */
export async function listTrash(
  f: HiddenFilter = {},
): Promise<HiddenPage<TrashedClip> & { trash_days: number | null; listed_at?: string }> {
  return apiFetch(`/trash?${hiddenQuery(f)}`);
}

/** Archived clips, most recently archived first. */
export async function listArchive(
  f: HiddenFilter & { kind?: "all" | "by_hand" | "missing" } = {},
): Promise<HiddenPage<ArchivedClip>> {
  return apiFetch(`/archive?${hiddenQuery(f, { kind: f.kind ?? "all" })}`);
}

/** What deleting clips for good would take them out of (a 409 in_projects's details). */
export interface ProjectUsage {
  assets_in_projects: number;
  projects: { project_id: string; name: string; status: string; in_trash: boolean; clips: number }[];
  other_projects: number;
}

export async function listDirectories(
  libraryId: string,
  parent?: string,
): Promise<DirectoryNode[]> {
  const qs = new URLSearchParams();
  if (parent) qs.set("parent", parent);
  return apiFetch<DirectoryNode[]>(
    `/libraries/${libraryId}/directories?${qs.toString()}`,
  );
}

/** Lightweight revision check for UI polling. */
export async function getLibraryRevision(
  libraryId: string,
): Promise<LibraryRevision> {
  return apiFetch<LibraryRevision>(`/libraries/${libraryId}/revision`);
}

/** Bulk health check — one row per non-trashed library, single SQL call.
 * `healthy` is `pending == 0`. Used to render a green/orange dot per
 * library on the libraries page without N+1ing repair-summary. */
export async function listLibraryHealth(): Promise<LibraryHealthItem[]> {
  return apiFetch<LibraryHealthItem[]>("/libraries/health");
}

// ---------- Unified Query (filter algebra) ----------

import type { LeafFilter } from "../lib/queryFilter";
import { ANALYSIS_PROXY, VIDEO_PREVIEW } from "../lib/names";

/** Response from GET /v1/query. */
export interface QueryItem {
  asset_id: string;
  library_id: string;
  library_name: string;
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
  iso: number | null;
  aperture: number | null;
  focal_length: number | null;
  focal_length_35mm: number | null;
  lens_model: string | null;
  flash_fired: boolean | null;
  gps_lat: number | null;
  gps_lon: number | null;
  face_count: number | null;
  thumbnail_key: string | null;
  proxy_key: string | null;
  created_at: string | null;
  /** Its last modified time: the most recent of every time related to it (sort "changed_at"). */
  changed_at?: string | null;
  search_context: {
    score: number;
    hit_type: string;
    snippet: string | null;
    start_ms: number | null;
    end_ms: number | null;
  } | null;
}

export interface QueryResponse {
  items: QueryItem[];
  next_cursor: string | null;
  total_estimate: number | null;
}

/** Unified query using filter algebra: GET /v1/query?f=prefix:value&... */
export async function queryAssets(
  filters: LeafFilter[],
  opts?: {
    sort?: string;
    dir?: "asc" | "desc";
    after?: string;
    limit?: number;
  },
): Promise<QueryResponse> {
  const params = new URLSearchParams();
  for (const f of filters) {
    params.append("f", `${f.type}:${f.value}`);
  }
  if (opts?.sort) params.set("sort", opts.sort);
  if (opts?.dir) params.set("dir", opts.dir);
  if (opts?.after) params.set("after", opts.after);
  if (opts?.limit) params.set("limit", String(opts.limit));
  return apiFetch<QueryResponse>(`/query?${params}`);
}

/** Fetch facets scoped by the active filter set. */
export async function getFilteredFacets(
  filters: LeafFilter[],
): Promise<FacetsResponse> {
  const params = new URLSearchParams();
  for (const f of filters) {
    params.append("f", `${f.type}:${f.value}`);
  }
  return apiFetch<FacetsResponse>(`/assets/facets?${params}`);
}

/** List detected faces for an asset. */
export async function listFaces(assetId: string): Promise<FaceListResponse> {
  return apiFetch<FaceListResponse>(`/assets/${assetId}/faces`);
}

// ---------- People ----------

export interface PersonItem {
  person_id: string;
  display_name: string;
  face_count: number;
  representative_face_id: string | null;
  representative_asset_id: string | null;
  confirmation_count: number;
}

export interface PersonListResponse {
  items: PersonItem[];
  next_cursor: string | null;
}

export interface PersonFaceItem {
  face_id: string;
  asset_id: string;
  bounding_box: { x: number; y: number; w: number; h: number } | null;
  detection_confidence: number | null;
  rel_path: string | null;
  taken_at?: string | null;
  media_type?: string | null;
  /** A video's face (the person seen there): the moment its crop is from, ms. */
  frame_ms?: number | null;
  /** Every moment they were seen at, ms, in order. */
  seen_ms?: number[] | null;
}

export interface PersonFacesResponse {
  items: PersonFaceItem[];
  next_cursor: string | null;
}

export interface ClusterItem {
  /** Names the cluster by its faces; a route given one that no longer matches answers 409 cluster_changed. */
  cluster_id: string;
  size: number;
  faces: PersonFaceItem[];
  /** Its latest photo (taken, else the file's time); null when none says. */
  newest?: string | null;
}

/** The orders unnamed clusters come in; ties go by the cluster's first face, so they hold between loads. */
export const CLUSTER_SORTS = ["size_desc", "size_asc", "newest"] as const;
export type ClusterSort = (typeof CLUSTER_SORTS)[number];

export interface ClustersResponse {
  clusters: ClusterItem[];
  truncated: boolean;
  max_cluster_size: number;
  /** When upkeep grouped them (null: not yet); reading them never does. */
  computed_at: string | null;
  /** Faces changed since: upkeep groups them again within minutes. */
  pending: boolean;
}

/** List people sorted by face count descending. */
export async function listPeople(cursor?: string, limit = 50): Promise<PersonListResponse> {
  const params = new URLSearchParams();
  if (cursor) params.set("after", cursor);
  params.set("limit", String(limit));
  return apiFetch<PersonListResponse>(`/people?${params}`);
}

/** Search people by name (typeahead). */
export async function searchPeople(q: string, limit = 10): Promise<PersonListResponse> {
  const params = new URLSearchParams({ q, limit: String(limit) });
  return apiFetch<PersonListResponse>(`/people?${params}`);
}

/** Get a person by ID. */
export async function getPerson(personId: string): Promise<PersonItem> {
  return apiFetch<PersonItem>(`/people/${personId}`);
}

/** Update a person's display name. */
export async function updatePerson(personId: string, displayName: string): Promise<PersonItem> {
  return apiFetch<PersonItem>(`/people/${personId}`, {
    method: "PATCH",
    body: { display_name: displayName },
  });
}

/** List dismissed people. */
export async function listDismissedPeople(cursor?: string, limit = 50): Promise<PersonListResponse> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor) params.set("after", cursor);
  return apiFetch<PersonListResponse>(`/people/dismissed?${params}`);
}

/** Get named people sorted by similarity to a person's centroid. */
export async function getNearestPeopleForPerson(personId: string, limit = 5): Promise<NearestPersonItem[]> {
  return apiFetch<NearestPersonItem[]>(`/people/${personId}/nearest?limit=${limit}`);
}

/** Restore a dismissed person and give them a name. */
export async function undismissPerson(personId: string, displayName: string): Promise<PersonItem> {
  return apiFetch<PersonItem>(`/people/${personId}/undismiss`, {
    method: "POST",
    body: { display_name: displayName },
  });
}

/** Delete a person and all their face matches. */
export async function deletePerson(personId: string): Promise<void> {
  await apiFetch<void>(`/people/${personId}`, { method: "DELETE" });
}

/** List faces matched to a person, cursor-paginated. */
export async function listPersonFaces(personId: string, cursor?: string, limit = 50): Promise<PersonFacesResponse> {
  const params = new URLSearchParams();
  if (cursor) params.set("after", cursor);
  params.set("limit", String(limit));
  return apiFetch<PersonFacesResponse>(`/people/${personId}/faces?${params}`);
}

/** Get face clusters (unassigned faces grouped by similarity). */
export async function getClusters(
  sort: ClusterSort,
  limit = 20,
  facesPerCluster = 6,
  minClusterSize = 2,
): Promise<ClustersResponse> {
  const params = new URLSearchParams({
    limit: String(limit),
    faces_per_cluster: String(facesPerCluster),
    min_cluster_size: String(minClusterSize),
    sort,
  });
  return apiFetch<ClustersResponse>(`/faces/clusters?${params}`);
}

/** Name a cluster: creates a new person or assigns to existing, for ALL faces in the cluster. */
export async function nameCluster(
  clusterId: string,
  opts: { displayName: string } | { personId: string; displayName: string },
): Promise<PersonItem> {
  const body: Record<string, unknown> = { display_name: opts.displayName };
  if ("personId" in opts) body.person_id = opts.personId;
  return apiFetch<PersonItem>(`/faces/clusters/${clusterId}/name`, {
    method: "POST",
    body,
  });
}

export interface NearestPersonItem {
  person_id: string;
  display_name: string;
  face_count: number;
  distance: number;
}

/** Get people sorted by similarity to a cluster's centroid. */
export async function getNearestPeople(clusterId: string, limit = 5): Promise<NearestPersonItem[]> {
  return apiFetch<NearestPersonItem[]>(`/faces/clusters/${clusterId}/nearest-people?limit=${limit}`);
}

/** Get people sorted by similarity to a single face's embedding. Used by
 * the lightbox face-assignment popover so the candidate dropdown is
 * ordered by who actually looks like the clicked face, instead of by
 * total face count (which has nothing to do with whether they match). */
export async function getNearestPeopleForFace(faceId: string, limit = 5): Promise<NearestPersonItem[]> {
  return apiFetch<NearestPersonItem[]>(`/faces/${faceId}/nearest-people?limit=${limit}`);
}

/** Dismiss a cluster: creates a dismissed person that absorbs future similar faces. Returns person_id for undo. */
export async function dismissCluster(clusterId: string): Promise<{ person_id: string }> {
  return apiFetch<{ person_id: string }>(`/faces/clusters/${clusterId}/dismiss`, {
    method: "POST",
  });
}

export interface ClusterFacesResponse {
  items: PersonFaceItem[];
  total: number;
  next_cursor: string | null;
}

/** List all faces in a cluster, paginated. */
export async function listClusterFaces(clusterId: string, cursor?: string, limit = 50): Promise<ClusterFacesResponse> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor) params.set("after", cursor);
  return apiFetch<ClusterFacesResponse>(`/faces/clusters/${clusterId}/faces?${params}`);
}

/** Assign a face to a person (existing or new). */
export async function assignFace(
  faceId: string,
  opts: { personId: string } | { newPersonName: string },
): Promise<{ person_id: string; display_name: string }> {
  const body: Record<string, string> =
    "personId" in opts ? { person_id: opts.personId } : { new_person_name: opts.newPersonName };
  return apiFetch(`/faces/${faceId}/assign`, {
    method: "POST",
    body,
  });
}

/** Remove a face from its assigned person. */
export async function unassignFace(faceId: string): Promise<void> {
  await apiFetch<void>(`/faces/${faceId}/assign`, { method: "DELETE" });
}

/** Merge source person into target person. Source is deleted. */
export async function mergePerson(targetPersonId: string, sourcePersonId: string): Promise<PersonItem> {
  return apiFetch<PersonItem>(`/people/${targetPersonId}/merge`, {
    method: "POST",
    body: { source_person_id: sourcePersonId },
  });
}

/** The query string a public page adds: its library, or its project. */
export function publicQuery(publicLibraryId?: string, publicProjectId?: string): string {
  if (publicProjectId) return `?public_project_id=${encodeURIComponent(publicProjectId)}`;
  if (publicLibraryId) return `?public_library_id=${encodeURIComponent(publicLibraryId)}`;
  return "";
}

export async function getAsset(assetId: string, publicLibraryId?: string, publicProjectId?: string): Promise<AssetDetail> {
  return apiFetch<AssetDetail>(`/assets/${assetId}${publicQuery(publicLibraryId, publicProjectId)}`);
}

/** Write a clip's description, OCR or tags (editors): the one copy, which the
 * machine never replaces; `tags` is the whole list. Only the fields sent change;
 * null removes what a person wrote (the machine makes it again). Returns the clip's detail. */
export async function correctAsset(
  assetId: string,
  body: { description?: string | null; ocr_text?: string | null; tags?: string[] | null },
): Promise<AssetDetail> {
  return apiFetch<AssetDetail>(`/assets/${assetId}/corrections`, { method: "PATCH", body });
}

/** A signed link a <video> element can stream and seek (no auth header needed). */
export interface Playback {
  url: string;
  expires_at: string;
  /** The full-length analysis proxy, or the 10-second preview until it exists. */
  source: typeof ANALYSIS_PROXY | typeof VIDEO_PREVIEW; // what plays, by its name (ADR-024)
  /** Seconds the link plays; null means the whole video. */
  max_seconds: number | null;
  /** False while the capped copy is made: ask again shortly (the link answers 503 until then). */
  ready: boolean;
  /** It's the whole clip: the analysis proxy, or the preview of a clip no longer than it. Older servers leave it out. */
  whole?: boolean;
}

export async function getPlayback(assetId: string, publicLibraryId?: string, publicProjectId?: string): Promise<Playback> {
  return apiFetch<Playback>(`/assets/${assetId}/playback${publicQuery(publicLibraryId, publicProjectId)}`);
}

export interface TenantSettings {
  /** Seconds of each video playback serves signed in; null means the whole video. */
  video_preview_max_seconds: number | null;
  /** The same on public pages, never more than the above; 10 until set. */
  public_video_preview_max_seconds: number | null;
  /** The same content is the same asset (moves, renames, copy then delete). On unless turned off; older servers leave it out. */
  follow_moves?: boolean;
  /** Days things stay in the trash before they're deleted for good; null when that's off. 30 unless changed. */
  trash_days?: number | null;
  /** Days a key a person made may go unused before it ends; null (the default) when keys end only when revoked. */
  key_idle_days?: number | null;
}

export async function getTenantSettings(): Promise<TenantSettings> {
  return apiFetch<TenantSettings>("/tenant/settings");
}

/** Change account settings (admins). Fewer trash_days answers 409
 * trash_days_shortened (with counts) when it would delete things at once,
 * until confirm_purge says yes; fewer key_idle_days 409 keys_would_end
 * (naming them) when keys would end at once, until confirm_end_keys does. */
export async function updateTenantSettings(
  update: Partial<TenantSettings> & { confirm_purge?: boolean; confirm_end_keys?: boolean },
): Promise<TenantSettings> {
  return apiFetch<TenantSettings>("/tenant/settings", { method: "PATCH", body: update });
}

/** The latest check of an AI machine (the worker's, or Connect's when it was saved). */
export interface MachineStatus {
  online: boolean;
  error: string;
  models: string[];
  checked_at: string | null;
}

/** A GPU machine the account's AI work runs on (Settings → AI). */
export interface AiMachine {
  machine_id: string;
  name: string;
  api_url: string;
  /** Whether a key is saved; the key itself is never sent back. */
  has_key: boolean;
  /** False for a saved key this install can't read (saved by another install: a backup restored here): enter it
   * again. */
  key_readable?: boolean;
  jobs: string[];
  /** Requests it takes at once. */
  at_once: number;
  enabled: boolean;
  /** The worker's own computer (its Whisper): no URL or key, can't be removed. */
  built_in: boolean;
  /** It shares the GPU video is decoded on here: it gets fewer requests while
   * video work runs there (the built-in one always does). */
  shares_gpu?: boolean;
  /** null until it has been checked. */
  status: MachineStatus | null;
}

/** A job and its one model; `machines` do it (enabled), `offering` offered the model when last checked. */
export interface AiJob {
  job: string;
  label: string;
  model: string;
  machines: number;
  offering: number;
  /** The models the machines doing it offer, to pick its model from. */
  choices: string[];
  /** The built-in machine can do it. */
  built_in: boolean;
  /** Why its model can't be changed (CLIP's: one model for a library's vectors); null when it can. */
  fixed?: string | null;
}

export interface AiSettings {
  machines: AiMachine[];
  jobs: AiJob[];
}

export async function getAiSettings(): Promise<AiSettings> {
  return apiFetch<AiSettings>("/ai");
}

/** Ask a machine which models it offers (admins). api_key undefined: a saved
 * machine's key (machine_id), when it's that machine's URL. 502
 * machine_unreachable says why when it can't. */
export async function connectMachine(body: { api_url: string; api_key?: string; machine_id?: string }): Promise<string[]> {
  return (await apiFetch<{ models: string[] }>("/ai/connect", { method: "POST", body })).models;
}

export interface MachineFields {
  name: string;
  api_url: string;
  /** undefined keeps the saved key (edits); "" none. */
  api_key?: string;
  jobs: string[];
  at_once: number;
  enabled?: boolean;
  shares_gpu?: boolean;
}

/** Add a machine (admins); it's asked for its models. 502 machine_unreachable,
 * 409 model_not_offered (details: job, model, models), 409 name_taken. */
export async function addMachine(body: MachineFields): Promise<AiSettings> {
  return apiFetch<AiSettings>("/ai/machines", { method: "POST", body });
}

/** Change a machine (admins): only the fields sent, as addMachine. Turning
 * off the last machine doing a job, or taking the job from it, never asks:
 * the job's work waits (Processing says why). The built-in machine takes no
 * api_url or api_key, and only the jobs it can do (409 built_in_machine). */
export async function updateMachine(machineId: string, body: Partial<MachineFields>): Promise<AiSettings> {
  return apiFetch<AiSettings>(`/ai/machines/${machineId}`, { method: "PATCH", body });
}

/** Remove a machine (admins); the last doing a job too (its work waits).
 * 409 built_in_machine for the built-in one (turn it off instead). */
export async function removeMachine(machineId: string): Promise<AiSettings> {
  return apiFetch<AiSettings>(`/ai/machines/${machineId}`, { method: "DELETE" });
}

/** Set a job's model (admins); "" turns the job off. Every machine doing it is
 * asked: 409 model_not_offered (details.machines: name, models, error) when none offers it.
 * A new model asks what becomes of what the old one made (ADR-018 decision 6):
 * 409 redo_on_change (details: model, clips, question "remake", artifacts[{artifact, title, clips}])
 * until `redo` says: true, remade too, after everything else; false, new files only. */
export async function setJobModel(job: string, model: string, redo?: boolean): Promise<AiSettings> {
  return apiFetch<AiSettings>(`/ai/jobs/${job}`, {
    method: "PUT",
    body: redo === undefined ? { model } : { model, redo },
  });
}

/** A producer's artifacts over the clips in sight (in one library or project when asked). */
export interface ProducerCounts {
  applicable: number;
  current: number;
  /** Made with another producer, model or settings than now. */
  stale: number;
  /** Not made yet. */
  missing: number;
  /** Made, and due to be checked again (location guesses): not missing. */
  rechecking?: number;
  /** Made below the producer's level now (scenes' Detail raised since): not current, not stale;
   * made again only when asked (redoBelow). */
  below?: number;
  /** The last try failed. */
  failing: number;
  /** Failing, and no longer tried (after 10 tries) until someone asks. */
  given_up?: number;
  /** Clips of its media it doesn't apply to, by what they lack (an installed plug-in's: no location). */
  lacking?: { name: string; label: string; clips: number }[];
}

/** What makes one kind of artifact (Settings → Processing). */
/** A producer's setting as Settings → Processing shows it. fixed: why it can't be changed here. */
export interface SettingField {
  key: string;
  label: string;
  // "notch": a quality, one of choices ("good", "better", "best"), not an encoder's number (ADR-018 decision 6).
  kind: "int" | "float" | "text" | "bool" | "level" | "notch";
  value: unknown;
  default: unknown;
  minimum: number | null;
  maximum: number | null;
  unit: string;
  advanced: boolean;
  fixed: string | null;
  /** Changing it makes the artifact again; false: it changes only what's done with what's made
   * (how faces are grouped). Absent: true. */
  remakes?: boolean;
  /** kind "level": its values, lowest first. Raising it makes nothing again by itself. */
  choices?: string[];
}

export interface Producer {
  artifact: string;
  version: string;
  title: string;
  media: string[];
  /** Its output must come from one model across the library. */
  uniform: boolean;
  settings: Record<string, unknown>;
  settings_hash: string;
  /** Its settings with their bounds; the ones its code reads can be changed (admins). */
  fields?: SettingField[];
  counts: ProducerCounts | null;
  /** The scheduler makes it; false: scans do (proxies, video previews), which can't be paused alone. */
  scheduled: boolean;
  /** Its stale clips are made again (after anything missing); if they can't be yet, why_not says why. */
  redoable: boolean;
  why_not: string | null;
  /** An admin stopped its redo, or a new version of it did (new_version): stale clips wait until it's resumed
   * (or its model changes). */
  redo_stopped: boolean;
  /** Its redo was stopped by a new version of it, not an admin: an admin's resume makes them again. */
  new_version?: boolean;
  /** Admins only. */
  redo_stopped_by: string | null;
  redo_stopped_at: string | null;
  /** An admin paused it: nothing more of it starts until it's resumed; what's running finishes. */
  paused: boolean;
  /** Admins only. */
  paused_by: string | null;
  paused_at: string | null;
  /** Why its work waits now: its AI job is off, or has no machine (or none online). */
  waiting: string | null;
  /** What its work grows with: seconds of video, or clips. */
  unit?: "second" | "clip";
  /** Its level now (its level setting's index, lowest 0); null without one. */
  level?: number | null;
  /** Shown as a part of another producer (its artifact): scenes are part of scene descriptions. */
  part_of?: string | null;
  /** The artifacts it's made from: a clip lacking one waits for it, and isn't in its counts. */
  needs?: string[];
  /** Why its due work waits now, each reason with how many clips (lib/waits.ts says each); null without counts. */
  waits?: ProducerWait[] | null;
  /** The plug-in it belongs to (ADR-021): it makes nothing while that's off in the account. */
  plugin?: { plugin_id: string; title: string; on?: boolean } | null;
}

/** Why some of a producer's due work waits (src/shared/waits.py): only the fields its reason has are sent. */
export interface ProducerWait {
  /** One of lib/waits.ts WAIT_REASONS (a newer server may send one this page doesn't know). */
  reason: string;
  clips: number;
  /** needs: the artifact it waits for. */
  artifact?: string;
  /** needs: what makes that is paused (a library's scans, for what scans make); scan: its library's scans are paused. */
  paused?: boolean;
  /** storage, device: the library; storage: what the scheduler's looks at its drive found. */
  library_id?: string;
  library_name?: string;
  seen_at?: string;
  away_since?: string;
  checked?: boolean;
  /** device: the device that reads the library, and when it last checked in for it. */
  device_id?: string;
  device_name?: string;
  checked_in_at?: string | null;
  remakes_held?: "battery" | "metered" | null;
  /** ai_machines: the job. */
  job?: string;
  /** ai_machines: "off" | "no_machine" | "offline" | "unusable"; scheduler: "not_running" | "starting" | "cant_list". */
  why?: string;
  /** ai_machines: why, in a sentence; scheduler (cant_list), scan_failing: the error. */
  message?: string;
  /** scan_failing: failing since, and when the library's scan is tried again. */
  since?: string;
  next_try?: string;
}

/** Make again what a producer made below its level now (admins): these clips (asset_ids) or every one
 * (all: true), one library's alone when given. 400 scope_required otherwise; 409 no_level. */
export async function redoBelow(
  artifact: string,
  body: { asset_ids?: string[]; all?: true; library_id?: string },
): Promise<{ redo: number; paused: boolean; redo_stopped: boolean }> {
  return apiFetch(`/producers/${artifact}/below/redo`, { method: "POST", body });
}

/** Change a producer's settings (admins): null puts one back to its default.
 * 409 redo_on_change (details.clips; details.question "remake") until redo says: true,
 * what the old settings made is remade too, after everything else; false, new files only
 * (ADR-018 decision 6).
 * 422 bad_setting, unknown_setting or setting_fixed say what's wrong. */
export async function setProducerSettings(
  artifact: string,
  settings: Record<string, unknown>,
  redo?: boolean,
): Promise<Producer> {
  return apiFetch<Producer>(`/producers/${artifact}/settings`, {
    method: "PUT",
    body: redo === undefined ? { settings } : { settings, redo },
  });
}

export async function getProducers(scope: { libraryId?: string; projectId?: string } = {}): Promise<Producer[]> {
  const qs = new URLSearchParams();
  if (scope.libraryId) qs.set("library_id", scope.libraryId);
  if (scope.projectId) qs.set("project_id", scope.projectId);
  const q = qs.toString();
  return (await apiFetch<{ producers: Producer[] }>(`/producers${q ? `?${q}` : ""}`)).producers;
}

/** What the scheduler is doing now (written every few seconds); live: it wrote in the last 30 s. */
export interface SchedulerStatus {
  live: boolean;
  at: string | null;
  running: Record<string, number>;
  waiting: Record<string, number>;
  /** Each pool's [busy, slots]. */
  pools: Record<string, [number, number]>;
  /** Requests AI machines sharing its GPU give up while video is decoded there. */
  gpu_hold: number;
  /** The account's pause state, derived from its switches and its libraries' scans: none paused, some, or all. */
  state: "running" | "partly" | "paused";
  /** Every pause switch: Upkeep, then each producer the scheduler makes. */
  switches: PauseSwitch[];
  /** Each library (not in the trash): who reads it and its own Scans pause. Older servers leave it out. */
  libraries?: LibraryScans[];
  /** How long until things are made; null or absent when it isn't running. */
  eta?: Eta | null;
}

/** One pause switch (Robert, Oct 9): "upkeep" or a producer's artifact. */
export interface PauseSwitch {
  target: string;
  title: string;
  paused: boolean;
  /** Admins only. */
  paused_by: string | null;
  paused_at: string | null;
  /** Why its work waits, as a producer's row says it. */
  waits?: ProducerWait[];
}

/** A library's scans (ADR-018 phase 1): who reads its files, and its own Scans pause (or Pause all's). */
export interface LibraryScans {
  library_id: string;
  name: string;
  /** "server": this server reads its files; else a device's id. */
  reader: string;
  /** The reader device's name; null for this server. */
  reader_name?: string | null;
  /** Its reader's latest check-in for it; none before the first. */
  checked_in?: ReaderCheckIn | null;
  paused: boolean;
  /** Admins only. */
  paused_by: string | null;
  paused_at: string | null;
  /** Why its scans wait: they keep failing. */
  waits?: ProducerWait[];
}

/** Seconds until each producer, pool and everything is caught up. A producer
 * with work and no time is in not_counted, saying why, and caught_up leaves it
 * out (null: nothing with work is counted). */
export interface Eta {
  producers: Record<string, number | null>;
  pools: Record<string, number>;
  caught_up: number | null;
  not_counted?: { artifact: string; title: string; why: "no_machine" | "not_known_yet" | "paused" }[];
  /** Jobs running now: their size in their producer's unit, seconds run and left, and whether well past its pace. */
  jobs: { kind: string; artifact: string | null; unit: "second" | "clip"; units: number; elapsed: number;
          left: number | null; late?: boolean }[];
}

export async function getSchedulerStatus(): Promise<SchedulerStatus> {
  return apiFetch<SchedulerStatus>("/producers/queue");
}

/** One part of the system on the Admin page: green, yellow or red, why, and where to fix it. */
export type HealthState = "green" | "yellow" | "red";

export interface HealthRow {
  key: "website" | "processing" | "ai" | "search" | "storage" | "disk";
  title: string;
  state: HealthState;
  reason: string;
  /** The page where it's fixed; null when there's none. */
  link: string | null;
  checked_at: string | null;
}

export interface SystemHealth {
  /** The worst row's. */
  state: HealthState;
  rows: HealthRow[];
  /** Whether the scheduler's latest look since it started reached each library's storage
   * (null: not known now: not looked at since, all processing paused, or the scheduler isn't running),
   * and when it was last seen reachable (null: never). */
  libraries: { library_id: string; name: string; reachable: boolean | null; seen_at: string | null }[];
  /** The Lumiverb running ("0.1.6", "v0.1.6-3-g1a2b3c4", "unknown"); older servers leave it out. */
  version?: string;
}

export async function getSystemHealth(): Promise<SystemHealth> {
  return apiFetch<SystemHealth>("/system/health");
}

/** The certificate other devices are served over HTTPS (Settings → Network). */
export interface NetworkCertificate {
  /** "lumiverb": its own authority's, which each device trusts once; "yours": one an admin gave. */
  source: "lumiverb" | "yours";
  names: string[];
  expires_at: string;
  /** Lumiverb's authority's certificate, for a device to trust (source "lumiverb"). */
  ca_url: string | null;
  /** Where another device fetches it: plain HTTP, as it can't trust HTTPS yet. */
  ca_addresses: string[];
  /** Its SHA-256 fingerprint, to compare on the device (it reached it over plain HTTP). */
  ca_fingerprint: string | null;
}

/** Whether other devices on the network may use a Mac's Lumiverb (Settings → Network). */
export interface NetworkAccess {
  /** Lumiverb runs this machine's web server (a Mac's install), so it's changed here. */
  available: boolean;
  /** Other devices may use it; otherwise this machine only. */
  other_devices: boolean;
  /** Other devices over HTTPS too (each trusts the certificate once). */
  https: boolean;
  /** Other devices over plain HTTP too: off only beside HTTPS, once a device has used it. */
  plain_http: boolean;
  /** Where other devices reach it over plain HTTP. */
  addresses: string[];
  /** And over HTTPS. */
  https_addresses: string[];
  /** The first device seen using HTTPS since it last changed; until one is, plain HTTP stays on. */
  https_seen: { at: string; address: string } | null;
  /** HTTPS's; null while it's off, and until Lumiverb has made one. */
  certificate: NetworkCertificate | null;
  /** Where it listens, the web and the API together (this Mac's own address too); null where it isn't available. */
  http_port: number | null;
  https_port: number | null;
}

/** What an admin changes in Settings → Network; each left out stays as it is. */
export interface NetworkChange {
  other_devices?: boolean;
  https?: boolean;
  plain_http?: boolean;
  /** 422 port_not_usable says why one can't be: the same twice, another of the install's, one in use. */
  http_port?: number;
  https_port?: number;
}

/** Whether other devices get plain HTTP: theirs unless HTTPS is on and an admin turned it off. */
export function httpForOthers(network: NetworkAccess): boolean {
  return network.other_devices && (network.plain_http || !network.https);
}

export async function getNetworkAccess(): Promise<NetworkAccess> {
  return apiFetch<NetworkAccess>("/system/network");
}

/** One record of the service's log (Admin › Logs) and the lines continuing it (a traceback). */
export interface LogEntry {
  id: string;
  /** api, scheduler, postgres, nginx, upkeep, lumiverb-server (a Mac's service itself). */
  part: string | null;
  level: "error" | "warning" | "info";
  text: string;
  /** The journal's time (Linux); a Mac's lines carry their own. */
  at: string | null;
}

export interface ServiceLog {
  /** file: a Mac's (launchd's); journal: Linux's; null: none to show (unavailable says why). */
  source: "file" | "journal" | null;
  where: string | null;
  unavailable: string | null;
  /** Newest first. */
  entries: LogEntry[];
  next_cursor: string | null;
}

export interface LogQuery {
  after?: string | null;
  part?: string | null;
  /** Warnings and errors only. */
  problems?: boolean;
  q?: string;
  limit?: number;
}

/** Admins; a server with more than one account only on the machine itself (403 logs_elsewhere). */
export async function getServiceLog(query: LogQuery = {}): Promise<ServiceLog> {
  const qs = new URLSearchParams();
  if (query.after) qs.set("after", query.after);
  if (query.part) qs.set("part", query.part);
  if (query.problems) qs.set("problems", "true");
  if (query.q) qs.set("q", query.q);
  if (query.limit) qs.set("limit", String(query.limit));
  const s = qs.toString();
  return apiFetch<ServiceLog>(`/system/logs${s ? `?${s}` : ""}`);
}

/** An admin's choice, on the machine itself (403 network_elsewhere otherwise; 409 https_not_seen
 * for plain HTTP off before a device has used HTTPS). */
export async function setNetworkAccess(change: NetworkChange): Promise<NetworkAccess> {
  return apiFetch<NetworkAccess>("/system/network", { method: "PUT", body: change });
}

/** An admin's own certificate and key (PEM), on the machine itself; 422 certificate_not_usable says why it can't be. */
export async function keepYourCertificate(certificate: string, key: string): Promise<NetworkAccess> {
  return apiFetch<NetworkAccess>("/system/network/certificate", { method: "PUT", body: { certificate, key } });
}

/** Back to Lumiverb's own certificate. */
export async function forgetYourCertificate(): Promise<NetworkAccess> {
  return apiFetch<NetworkAccess>("/system/network/certificate", { method: "DELETE" });
}

/** A backup of every database (Settings → Backups). */
export interface BackupItem {
  name: string;
  made_at: string;
  /** nightly | now | before-upgrade | before-restore */
  kind: string;
  bytes: number;
  accounts: number;
  /** The commands that put it back, in order, on the machine Lumiverb runs on. */
  restore: string[];
}

export interface Backups {
  folder: string;
  /** Lumiverb can't write there now: why. */
  folder_write_error?: string | null;
  default_folder: string;
  /** The folder is chosen on the machine itself. */
  changeable_here: boolean;
  running: { kind: string; started_at: string | null; done: number; of: number } | null;
  last_error: string | null;
  last_failed_at: string | null;
  /** When the nightly one is made next (now or past: within minutes). */
  next_at: string;
  /** Newest first. */
  backups: BackupItem[];
}

export async function getBackups(): Promise<Backups> {
  return apiFetch<Backups>("/system/backups");
}

/** Starts one now (409 backup_running while one is being made). */
export async function backUpNow(): Promise<Backups> {
  return apiFetch<Backups>("/system/backups", { method: "POST" });
}

/** An admin's choice, on the machine itself; null: back to the default. One not there: a 409 make_folder, unless makeFolder. */
export async function setBackupsFolder(folder: string | null, makeFolder = false): Promise<Backups> {
  return apiFetch<Backups>("/system/backups/folder", { method: "PUT", body: { folder, make_folder: makeFolder } });
}

/** A folder not there yet: the 409 make_folder's details, for the user to say make it. */
export interface NewFolderFacts {
  folder: string;
  free: number;
  same_drive_as_data: boolean;
}

/** Where the previews are, and a move under way (Settings → Storage). */
export interface StorageMove {
  /** One library's previews (else the server's). */
  library_id?: string | null;
  library_name?: string | null;
  from_folder: string;
  to_folder: string;
  done_files: number;
  total_files: number;
  done_bytes: number;
  total_bytes: number;
  running: boolean;
  /** Why it stopped; upkeep tries again 15 minutes later, or Try again now. */
  error: string | null;
  started_at: string | null;
}

export interface StorageInfo {
  previews: string;
  /** The data folder: the database, the search index, models and caches (they don't move). */
  data: string;
  /** The previews' drive is plugged in. */
  previews_available: boolean;
  /** Lumiverb can't write there now: why. */
  previews_write_error?: string | null;
  previews_free: number | null;
  previews_total: number | null;
  data_free: number | null;
  data_total: number | null;
  same_disk: boolean;
  moving: StorageMove | null;
  /** Moved only on the machine itself. */
  changeable_here: boolean;
  /** The account's libraries with a folder of their own. */
  libraries: { library_id: string; name: string; folder: string; available: boolean; write_error?: string | null }[];
}

/** Where a library's previews are: the server's folder, or one of its own (Library settings). */
export interface LibraryPreviews {
  library_id: string;
  folder: string;
  own: boolean;
  server_folder: string;
  available: boolean;
  /** Lumiverb can't write there now: why. */
  write_error?: string | null;
  free: number | null;
  total: number | null;
  /** On the drive its originals are on (null: they aren't reachable here). */
  same_drive_as_originals: boolean | null;
  moving: Omit<StorageMove, "library_id" | "library_name"> | null;
  changeable_here: boolean;
}

export async function getLibraryPreviews(libraryId: string): Promise<LibraryPreviews> {
  return apiFetch<LibraryPreviews>(`/libraries/${encodeURIComponent(libraryId)}/previews`);
}

/** Without move, a 409 move_previews with the facts (unless there's nothing to move or say); with it, the move
 * starts. A folder not there: a 409 make_folder first, unless makeFolder. null: back to the server's folder. */
export async function moveLibraryPreviews(
  libraryId: string, folder: string | null, move: boolean, makeFolder = false,
): Promise<LibraryPreviews> {
  return apiFetch<LibraryPreviews>(`/libraries/${encodeURIComponent(libraryId)}/previews`, {
    method: "PUT", body: { folder, move, make_folder: makeFolder },
  });
}

/** What a move carries: the 409 move_previews's details, for the user to confirm. */
export interface PreviewsMoveFacts {
  from: string;
  to: string;
  files: number;
  bytes: number;
  free: number;
  /** A library's: the drive its originals are on. */
  same_drive_as_originals?: boolean;
}

export async function getStorageInfo(): Promise<StorageInfo> {
  return apiFetch<StorageInfo>("/system/storage");
}

/** Without move, a 409 move_previews with the facts (unless there's nothing to move); with it, the move starts
 * (202). A folder not there: a 409 make_folder first, unless makeFolder. null: back to the data folder. */
export async function movePreviews(folder: string | null, move: boolean, makeFolder = false): Promise<StorageInfo> {
  return apiFetch<StorageInfo>("/system/storage/previews", {
    method: "PUT", body: { folder, move, make_folder: makeFolder },
  });
}

/** Try again: a move that stopped goes on now. */
export async function goOnMovingPreviews(): Promise<StorageInfo> {
  return apiFetch<StorageInfo>("/system/storage/move", { method: "POST" });
}

/** How far the account's admins got through setting it up (the /welcome wizard). */
export interface SetupProgress {
  steps: string[];
  done: string[];
  /** Put away: the Admin page stops offering it. */
  dismissed: boolean;
  finished: boolean;
}

export async function getSetupProgress(): Promise<SetupProgress> {
  return apiFetch<SetupProgress>("/tenant/setup");
}

export async function setSetupProgress(change: { done?: string[]; dismissed?: boolean }): Promise<SetupProgress> {
  return apiFetch<SetupProgress>("/tenant/setup", { method: "PUT", body: change });
}

/** A clip whose last try failed (Settings → Processing). */
export interface FailingClip {
  asset_id: string;
  artifact: string;
  title: string;
  rel_path: string;
  library_id: string;
  library_name: string;
  media_type: string;
  error: string;
  attempts: number;
  failed_at: string | null;
  /** When it's tried again; null once it's given up. */
  retry_at: string | null;
  given_up: boolean;
}

/** Clips whose last try failed, newest failure first (signed in). */
export async function getFailures(params: { artifact?: string; libraryId?: string; after?: string; limit?: number } = {}):
    Promise<{ items: FailingClip[]; next_cursor: string | null }> {
  const qs = new URLSearchParams();
  if (params.artifact) qs.set("artifact", params.artifact);
  if (params.libraryId) qs.set("library_id", params.libraryId);
  if (params.after) qs.set("after", params.after);
  if (params.limit) qs.set("limit", String(params.limit));
  const q = qs.toString();
  return apiFetch(`/producers/failures${q ? `?${q}` : ""}`);
}

/** Try failing clips again now, given up or not (editors and admins): these clips (asset_ids) or every
 * failing one (all: true), a producer's or a library's alone when given. 400 scope_required otherwise. */
export async function retryFailures(body: { artifact?: string; library_id?: string; asset_ids?: string[]; all?: true }):
    Promise<{ retried: number }> {
  return apiFetch(`/producers/failures/retry`, { method: "POST", body });
}

/** Stop redoing a producer's stale clips (admins): its switch's redo scope; what's missing is still made.
 * 409 cant_redo. */
export async function stopRedo(artifact: string): Promise<void> {
  await apiFetch<void>(`/producers/${artifact}/pause`, { method: "POST", body: { scope: "redo" } });
}

/** Redo a producer's stale clips again (admins), after anything missing. */
export async function resumeRedo(artifact: string): Promise<void> {
  await apiFetch<void>(`/producers/${artifact}/resume`, { method: "POST", body: { scope: "redo" } });
}

/** Pause one switch's work ("upkeep" or a producer's artifact), or "all" (admins). The target is
 * always named. What's running finishes. 409 not_scheduled for what scans make (a library's scans pause it). */
export async function pauseProcessing(target: string): Promise<void> {
  await apiFetch<void>(`/producers/${target}/pause`, { method: "POST", body: { scope: "work" } });
}

/** Resume one switch's work, or "all" (admins). */
export async function resumeProcessing(target: string): Promise<void> {
  await apiFetch<void>(`/producers/${target}/resume`, { method: "POST", body: { scope: "work" } });
}

/** Pause these libraries' scans, or every library's ("all", named, never inferred) (admins): no new or
 * changed files are found in them; everything else goes on. */
export async function pauseScans(libraries: string[] | "all"): Promise<void> {
  await apiFetch<void>(`/libraries/scans/pause`, {
    method: "POST", body: libraries === "all" ? { all: true } : { library_ids: libraries } });
}

/** Scan these libraries again, or every library (admins). */
export async function resumeScans(libraries: string[] | "all"): Promise<void> {
  await apiFetch<void>(`/libraries/scans/resume`, {
    method: "POST", body: libraries === "all" ? { all: true } : { library_ids: libraries } });
}

/** Ask the library's reader for a scan of the whole library, at its next check-in (editors). 409 scans_paused
 * while its scans are paused; asked again before it's done, the first ask stands. */
export async function scanNow(libraryId: string): Promise<void> {
  await apiFetch<void>(`/libraries/${encodeURIComponent(libraryId)}/scan-now`, { method: "POST" });
}

export async function findSimilar(params: {
  assetId: string;
  libraryId: string;
  limit?: number;
  pathPrefix?: string;
}): Promise<SimilarityResponse> {
  const qs = new URLSearchParams({
    asset_id: params.assetId,
    library_id: params.libraryId,
  });
  if (params.limit) qs.set("limit", String(params.limit));
  return apiFetch<SimilarityResponse>(`/similar?${qs.toString()}`);
}

export interface PathFilterItem {
  filter_id: string;
  pattern: string;
  created_at: string;
}

export interface LibraryFiltersResponse {
  includes: PathFilterItem[];
  excludes: PathFilterItem[];
}

export interface CreatedFilterResponse {
  filter_id: string;
  type: string;
  pattern: string;
  created_at: string;
  trashed_count?: number;
}

export interface PreviewFilterResponse {
  matching_asset_count: number;
}

export async function getLibraryFilters(
  libraryId: string,
): Promise<LibraryFiltersResponse> {
  return apiFetch<LibraryFiltersResponse>(
    `/libraries/${libraryId}/filters`,
  );
}

/** Add a path filter. An exclude filter with trashMatching moves the clips it
 * matches to the trash: 409 in_projects first when projects use them, unless
 * removeFromProjects says the user agreed (nothing changes until then). */
export async function addLibraryFilter(
  libraryId: string,
  type: "include" | "exclude",
  pattern: string,
  trashMatching = false,
  removeFromProjects = false,
): Promise<CreatedFilterResponse> {
  return apiFetch<CreatedFilterResponse>(
    `/libraries/${libraryId}/filters`,
    { method: "POST", body: { type, pattern, trash_matching: trashMatching, remove_from_projects: removeFromProjects } },
  );
}

export async function previewLibraryFilter(
  libraryId: string,
  type: "include" | "exclude",
  pattern: string,
): Promise<PreviewFilterResponse> {
  return apiFetch<PreviewFilterResponse>(
    `/libraries/${libraryId}/filters/preview`,
    { method: "POST", body: { type, pattern } },
  );
}

export async function deleteLibraryFilter(
  libraryId: string,
  filterId: string,
): Promise<void> {
  return apiFetch<void>(`/libraries/${libraryId}/filters/${filterId}`, {
    method: "DELETE",
  });
}

export async function listUsers(): Promise<UserItem[]> {
  return apiFetch<UserItem[]>("/users");
}

/** Invite someone: they get a link (invite_urls) to choose their own password. everyLibrary false: they see
 * only the libraries they're given a role on. */
export async function inviteUser(email: string, role: string, everyLibrary = true): Promise<UserItem> {
  return apiFetch<UserItem>("/users", {
    method: "POST",
    body: { email, role, ...(everyLibrary ? {} : { every_library: false }) },
  });
}

/** Whether someone sees every library (their account role on each), or only the ones they're given a role on. */
export async function setUserEveryLibrary(userId: string, everyLibrary: boolean): Promise<UserItem> {
  return apiFetch<UserItem>(`/users/${userId}`, { method: "PATCH", body: { every_library: everyLibrary } });
}

/** A new link for someone who hasn't signed in yet; the old one stops working (409 already_joined otherwise). */
export async function newInviteLink(userId: string): Promise<InviteLink> {
  return apiFetch<InviteLink>(`/users/${encodeURIComponent(userId)}/invite`, { method: "POST" });
}

export async function updateUserRole(
  userId: string,
  role: string,
): Promise<UserItem> {
  return apiFetch<UserItem>(`/users/${userId}`, {
    method: "PATCH",
    body: { role },
  });
}

export async function deleteUser(userId: string): Promise<void> {
  return apiFetch<void>(`/users/${userId}`, { method: "DELETE" });
}

// --- The install's accounts and server admins (a server admin's: Admin › Accounts) ---

export async function listAccounts(): Promise<AccountItem[]> {
  return apiFetch<AccountItem[]>("/admin/tenants");
}

/** An account, its first admin invited at email (the link comes back, once). 409 email_conflict. */
export async function createAccount(name: string, email: string): Promise<AccountMade> {
  return apiFetch<AccountMade>("/admin/tenants", { method: "POST", body: { name, email } });
}

export async function renameAccount(tenantId: string, name: string): Promise<void> {
  await apiFetch(`/admin/tenants/${encodeURIComponent(tenantId)}`, { method: "PATCH", body: { name } });
}

/** 409 confirm_delete_account (details: name, users, libraries, clips) until confirm is its name; own_account and
 * last_server_admin 409s too. */
export async function deleteAccount(tenantId: string, confirm?: string): Promise<void> {
  const query = confirm === undefined ? "" : `?confirm=${encodeURIComponent(confirm)}`;
  return apiFetch<void>(`/admin/tenants/${encodeURIComponent(tenantId)}${query}`, { method: "DELETE" });
}

export async function listServerAdmins(): Promise<ServerAdminItem[]> {
  return (await apiFetch<{ server_admins: ServerAdminItem[] }>("/admin/server-admins")).server_admins;
}

/** 404 user_not_found when nobody signs in here with that email. */
export async function addServerAdmin(email: string): Promise<ServerAdminItem[]> {
  return (await apiFetch<{ server_admins: ServerAdminItem[] }>("/admin/server-admins",
                                                               { method: "POST", body: { email } })).server_admins;
}

/** 409 last_server_admin for the last one. */
export async function removeServerAdmin(userId: string): Promise<void> {
  return apiFetch<void>(`/admin/server-admins/${encodeURIComponent(userId)}`, { method: "DELETE" });
}

export async function getCurrentUser(): Promise<CurrentUser> {
  return apiFetch<CurrentUser>("/me");
}

export async function listApiKeys(): Promise<ApiKeyItem[]> {
  const res = await apiFetch<{ keys: ApiKeyItem[] }>("/keys");
  return res.keys;
}

export async function createApiKey(label: string, role?: string): Promise<ApiKeyCreateResponse> {
  const body: Record<string, string> = { label };
  if (role) body.role = role;
  return apiFetch<ApiKeyCreateResponse>("/keys", {
    method: "POST",
    body,
  });
}

export async function revokeApiKey(keyId: string): Promise<void> {
  return apiFetch<void>(`/keys/${keyId}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// Projects
// ---------------------------------------------------------------------------

export type ProjectStatus = "active" | "archived";
/** "all" is active and archived; "trashed" is your own trash. */
export type ProjectView = ProjectStatus | "all" | "trashed";

/** Active projects by default; archived ones leave the sidebar and pickers,
 * and trashed ones appear only in the trash. */
export async function listProjects(
  status: ProjectView = "active",
): Promise<ProjectItem[]> {
  const qs = status === "active" ? "" : `?status=${status}`;
  const res = await apiFetch<ProjectListResponse>(`/projects${qs}`);
  return res.items;
}

/** Archive (or restore) a project. Archived projects keep their clips. */
export async function setProjectStatus(
  projectId: string,
  status: ProjectStatus,
): Promise<ProjectItem> {
  return apiFetch<ProjectItem>(`/projects/${projectId}`, {
    method: "PATCH",
    body: { status },
  });
}

export async function getProject(
  projectId: string,
): Promise<ProjectItem> {
  return apiFetch<ProjectItem>(`/projects/${projectId}`);
}

export async function createProject(
  name: string,
  opts?: {
    description?: string;
    sort_order?: string;
    visibility?: string;
    asset_ids?: string[];
    /** A search saved as a project: the clips it matches now (filter JSON, as buildSavedQuery makes). */
    from_search?: Record<string, unknown>;
  },
): Promise<ProjectItem> {
  return apiFetch<ProjectItem>("/projects", {
    method: "POST",
    body: { name, ...opts },
  });
}

export async function updateProject(
  projectId: string,
  body: {
    name?: string;
    description?: string | null;
    visibility?: string;
    sort_order?: string;
    cover_asset_id?: string | null;
  },
): Promise<ProjectItem> {
  return apiFetch<ProjectItem>(`/projects/${projectId}`, {
    method: "PATCH",
    body,
  });
}

/** Move a project to the trash. Restore brings it back as it was. */
export async function trashProject(projectId: string): Promise<void> {
  return apiFetch<void>(`/projects/${projectId}`, { method: "DELETE" });
}

/** Take a project out of the trash, back to active or archived as it was.
 * withClips says what to do with its clips in the trash; the server refuses
 * (409 clips_in_trash) without it when there are some. */
export async function restoreProject(
  projectId: string,
  withClips?: boolean,
): Promise<{ restored_clips: number; trashed_clips: number; missing_clips: number }> {
  return apiFetch<{ restored_clips: number; trashed_clips: number; missing_clips: number }>(`/projects/${projectId}/restore`, {
    method: "POST",
    body: withClips === undefined ? {} : { with_clips: withClips },
  });
}

/** Delete these trashed projects for good. Their clips stay in the library. */
export async function emptyProjectTrash(projectIds: string[]): Promise<{ deleted: number }> {
  return apiFetch<{ deleted: number }>("/projects/empty-trash", {
    method: "POST",
    body: { project_ids: projectIds },
  });
}

/** Restore the clips in a project that someone trashed. They come back
 * everywhere; clips whose files went missing are only counted. */
export async function restoreProjectClips(
  projectId: string,
): Promise<{ restored: number; missing: number }> {
  return apiFetch<{ restored: number; missing: number }>(`/projects/${projectId}/restore-clips`, {
    method: "POST",
  });
}

export async function listProjectAssets(
  projectId: string,
  after?: string,
  limit = 200,
): Promise<ProjectAssetsResponse> {
  const qs = new URLSearchParams();
  if (after) qs.set("after", after);
  qs.set("limit", String(limit));
  return apiFetch<ProjectAssetsResponse>(
    `/projects/${projectId}/assets?${qs.toString()}`,
  );
}

export async function addAssetsToProject(
  projectId: string,
  assetIds: string[],
): Promise<BatchAddResponse> {
  return apiFetch<BatchAddResponse>(`/projects/${projectId}/assets`, {
    method: "POST",
    body: { asset_ids: assetIds },
  });
}

export async function removeAssetsFromProject(
  projectId: string,
  assetIds: string[],
): Promise<BatchRemoveResponse> {
  return apiFetch<BatchRemoveResponse>(`/projects/${projectId}/assets`, {
    method: "DELETE",
    body: { asset_ids: assetIds },
  });
}

// ---------------------------------------------------------------------------
// Ratings
// ---------------------------------------------------------------------------

export async function rateAsset(
  assetId: string,
  body: { favorite?: boolean; stars?: number; color?: string | null },
): Promise<RatingResponse> {
  return apiFetch<RatingResponse>(`/assets/${assetId}/rating`, {
    method: "PUT",
    body,
  });
}

export async function batchRateAssets(
  assetIds: string[],
  body: { favorite?: boolean; stars?: number; color?: string | null },
): Promise<BatchRatingResponse> {
  return apiFetch<BatchRatingResponse>("/assets/ratings", {
    method: "PUT",
    body: { asset_ids: assetIds, ...body },
  });
}

export async function lookupRatings(
  assetIds: string[],
): Promise<RatingLookupResponse> {
  return apiFetch<RatingLookupResponse>("/assets/ratings/lookup", {
    method: "POST",
    body: { asset_ids: assetIds },
  });
}

// ---------------------------------------------------------------------------
// Saved Views (ADR-008)
// ---------------------------------------------------------------------------

export interface SavedViewItem {
  view_id: string;
  name: string;
  query_params: string;
  icon: string | null;
  position: number;
  created_at: string;
  updated_at: string;
}

export async function listSavedViews(): Promise<{ items: SavedViewItem[] }> {
  return apiFetch("/views");
}

export async function createSavedView(
  name: string,
  queryParams: string,
  icon?: string | null,
): Promise<SavedViewItem> {
  return apiFetch<SavedViewItem>("/views", {
    method: "POST",
    body: { name, query_params: queryParams, icon: icon ?? null },
  });
}

export async function updateSavedView(
  viewId: string,
  body: { name?: string; query_params?: string; icon?: string | null },
): Promise<SavedViewItem> {
  return apiFetch<SavedViewItem>(`/views/${viewId}`, {
    method: "PATCH",
    body,
  });
}

export async function deleteSavedView(viewId: string): Promise<void> {
  return apiFetch<void>(`/views/${viewId}`, { method: "DELETE" });
}

/** Upload or replace an SRT transcript for a video asset. */
export async function uploadTranscript(
  assetId: string,
  srt: string,
  language?: string,
): Promise<{ asset_id: string; status: string }> {
  return apiFetch<{ asset_id: string; status: string }>(
    `/assets/${assetId}/transcript`,
    { method: "POST", body: { srt, language: language ?? null, source: "manual" } },
  );
}

/** Remove the transcript a video shows, saying whose: a person's ("manual"; the
 * machine makes one again) or the machine's (for good). 409 transcript_changed
 * when it shows the other one. */
export async function deleteTranscript(assetId: string, which: "manual" | "machine"): Promise<void> {
  await apiFetch<void>(`/assets/${assetId}/transcript?which=${which}`, { method: "DELETE" });
}

/** Add or update a note on an asset. */
export async function updateNote(assetId: string, text: string): Promise<void> {
  await apiFetch<void>(`/assets/${assetId}/note`, { method: "PUT", body: { text } });
}

/** Delete a note from an asset. */
export async function deleteNote(assetId: string): Promise<void> {
  await apiFetch<void>(`/assets/${assetId}/note`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// Locations (ADR-017): always for named clips
// ---------------------------------------------------------------------------

export type LocationReplace = "none" | "person" | "all";

/** A person's location for these clips: a point, or the same place as another clip.
 * Replacing a location the file or a person gave answers 409 location_exists
 * (details: count, person, file) until `replace` says so. */
export async function setLocations(
  body: { asset_ids: string[]; replace?: LocationReplace }
    & ({ lat: number; lon: number } | { same_as: string } | { place_id: string }),
): Promise<{ updated: string[]; skipped: string[] }> {
  return apiFetch("/assets/locations", { method: "PUT", body });
}

/** Places whose name starts with q ("Name, region or country" narrows it): every plug-in's on here that finds
 * places (ADR-023), each with its source. 409 plugin_off when none is on (its message says why, details.plugins
 * those installed that would). With the credits to show beside them, and the plug-ins that couldn't answer now
 * (not running, fetching their first list). */
export async function searchPlaces(q: string, limit = 8): Promise<PlaceSearch> {
  return apiFetch(`/places?${new URLSearchParams({ q, limit: String(limit) })}`);
}

/** Clear a person's location from these clips (the file's GPS stays). */
export async function clearLocations(assetIds: string[]): Promise<{ cleared: string[]; skipped: string[] }> {
  return apiFetch("/assets/locations", { method: "DELETE", body: { asset_ids: assetIds } });
}

/** These clips' suggestions become a person's location. */
export async function acceptLocations(assetIds: string[]): Promise<{ accepted: string[]; skipped: string[] }> {
  return apiFetch("/assets/locations/accept", { method: "POST", body: { asset_ids: assetIds } });
}


// ---------------------------------------------------------------------------
// Send to editor
// ---------------------------------------------------------------------------

export interface ExportFormat {
  id: string;
  label: string;
  file_extension: string;
}

export async function listExportFormats(): Promise<ExportFormat[]> {
  const res = await apiFetch<{ items: ExportFormat[] }>("/export/formats");
  return res.items;
}

export interface ProjectExportFile {
  blob: Blob;
  filename: string;
  /** Photos in the project, exported as stills. */
  stills: number;
  /** Videos with no known length, left out (a zero-length clip breaks Final Cut). */
  skippedNoDuration: number;
  /** Videos exported at a fallback frame rate because they haven't been probed. */
  unprobed: number;
  /** Clips someone trashed, left out until restored. */
  skippedTrashed: number;
  /** Clips whose files went missing, left out until they're back. */
  skippedMissing: number;
  /** Clips whose library is in the trash, left out. */
  skippedLibraryTrashed: number;
  skippedArchived: number;
}

function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1]);
    } catch {
      /* fall through to the plain form */
    }
  }
  const plain = /filename="([^"]+)"/i.exec(header);
  return plain ? plain[1] : null;
}

/** Export a project as a bin of master clips for an editor: videos, and photos as
 * stills, pointing at the originals as the libraries know them (relinked in the editor). */
export async function exportProject(projectId: string, format: string): Promise<ProjectExportFile> {
  const qs = new URLSearchParams({ format });
  const res = await authFetch(`/projects/${projectId}/export?${qs.toString()}`);
  if (!res.ok) {
    let message = `Export failed (${res.status})`;
    try {
      const body = await res.json();
      message = body?.error?.message ?? message;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, message);
  }
  return {
    blob: await res.blob(),
    filename: filenameFromDisposition(res.headers.get("Content-Disposition")) ?? "project-export",
    stills: Number(res.headers.get("X-Lumiverb-Stills") ?? 0) || 0,
    skippedNoDuration: Number(res.headers.get("X-Lumiverb-Skipped-No-Duration") ?? 0) || 0,
    unprobed: Number(res.headers.get("X-Lumiverb-Unprobed") ?? 0) || 0,
    skippedTrashed: Number(res.headers.get("X-Lumiverb-Skipped-Trashed") ?? 0) || 0,
    skippedMissing: Number(res.headers.get("X-Lumiverb-Skipped-Missing") ?? 0) || 0,
    skippedLibraryTrashed: Number(res.headers.get("X-Lumiverb-Skipped-Library-Trashed") ?? 0) || 0,
    skippedArchived: Number(res.headers.get("X-Lumiverb-Skipped-Archived") ?? 0) || 0,
  };
}

const DEFAULT_EXPORT_FORMAT_KEY = "lumiverb.defaultExportFormat";

/** The format the Export button uses without asking; null until the user picks one. */
export function getDefaultExportFormat(): string | null {
  try {
    return localStorage.getItem(DEFAULT_EXPORT_FORMAT_KEY);
  } catch {
    return null;
  }
}

/** Remember a format for one-click Export. */
export function setDefaultExportFormat(format: string | null): void {
  try {
    if (format) localStorage.setItem(DEFAULT_EXPORT_FORMAT_KEY, format);
    else localStorage.removeItem(DEFAULT_EXPORT_FORMAT_KEY);
  } catch {
    /* storage unavailable: the chooser just opens each time */
  }
}
