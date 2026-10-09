import type {
  ApiKeyCreateResponse,
  ApiKeyItem,
  AssetDetail,
  AssetPageItem,
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
  LibraryHealthItem,
  LibraryListItem,
  LibraryResponse,
  LibraryRevision,
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

const authHeaders = (): HeadersInit => {
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

export function handleUnauthorized(): void {
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

type ApiFetchOptions = Omit<RequestInit, "body"> & { body?: unknown };

async function apiFetch<T>(
  path: string,
  options?: ApiFetchOptions,
): Promise<T> {
  const { body, ...rest } = options ?? {};
  const headers: HeadersInit = {
    "Content-Type": "application/json",
    ...authHeaders(),
    ...rest.headers,
  };
  const fetchBody =
    body !== undefined ? JSON.stringify(body) : (rest as RequestInit).body;
  const res = await fetch(`/v1${path}`, {
    ...rest,
    headers,
    body: fetchBody,
  });
  if (!res.ok) {
    if (res.status === 401) {
      // Try silent refresh before giving up
      const refreshed = await tryRefresh();
      if (refreshed) {
        // Retry the original request with the new token
        const retryHeaders: HeadersInit = {
          "Content-Type": "application/json",
          ...authHeaders(),
          ...rest.headers,
        };
        const retryBody =
          body !== undefined ? JSON.stringify(body) : (rest as RequestInit).body;
        const retryRes = await fetch(`/v1${path}`, {
          ...rest,
          headers: retryHeaders,
          body: retryBody,
        });
        if (retryRes.ok) {
          if (retryRes.status === 204) return null as T;
          return retryRes.json() as Promise<T>;
        }
      }
      handleUnauthorized();
    }
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
      // ignore
    }
    throw new ApiError(res.status, message, code, details);
  }
  if (res.status === 204) {
    return null as T;
  }
  return res.json() as Promise<T>;
}

/** Fetch blob (e.g. image) with auth. Used for thumbnail/proxy URLs. */
export async function apiFetchBlob(path: string): Promise<Blob> {
  let res = await fetch(`/v1${path}`, { headers: authHeaders() });
  if (!res.ok) {
    if (res.status === 401) {
      const refreshed = await tryRefresh();
      if (refreshed) {
        res = await fetch(`/v1${path}`, { headers: authHeaders() });
        if (res.ok) return res.blob();
      }
      handleUnauthorized();
    }
    throw new ApiError(res.status, res.statusText);
  }
  return res.blob();
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
): Promise<LibraryResponse> {
  return apiFetch<LibraryResponse>("/libraries", {
    method: "POST",
    body: { name, root_path: rootPath },
  });
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

export async function updateLibraryVisibility(
  libraryId: string,
  is_public: boolean,
): Promise<LibraryResponse> {
  return apiFetch<LibraryResponse>(`/libraries/${libraryId}`, {
    method: "PATCH",
    body: { is_public },
  });
}

/** Delete trashed libraries for good: these ones, or all of them. If their
 * clips are in projects, the server refuses (409 in_projects, with the
 * projects) unless removeFromProjects says the user agreed. */
export async function emptyTrash(removeFromProjects = false, libraryIds?: string[]): Promise<EmptyTrashResponse> {
  return apiFetch<EmptyTrashResponse>("/libraries/empty-trash", {
    method: "POST",
    body: { remove_from_projects: removeFromProjects, ...(libraryIds ? { library_ids: libraryIds } : {}) },
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

/** Delete clips in the trash for good now: these, or every one shown (a
 * library's, under a folder, when given). Admins. 409 in_projects until
 * removeFromProjects. Never reaches archived clips or a trashed library's. */
export async function emptyClipTrash(
  assetIds?: string[],
  removeFromProjects = false,
  scope: { libraryId?: string; path?: string; trashedBefore?: string } = {},
): Promise<{ deleted: number }> {
  return apiFetch("/trash/empty", {
    method: "DELETE",
    body: {
      ...(assetIds ? { asset_ids: assetIds } : {}),
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

/** Fetch aggregated filter facets for a library (legacy — use getFilteredFacets). */
export async function getFacets(
  libraryId: string,
  pathPrefix?: string,
): Promise<FacetsResponse> {
  const params = new URLSearchParams({ library_id: libraryId });
  if (pathPrefix) params.set("path_prefix", pathPrefix);
  return apiFetch<FacetsResponse>(`/assets/facets?${params}`);
}

// ---------- Unified Query (filter algebra) ----------

import type { LeafFilter, FilterCapability } from "../lib/queryFilter";

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

/** Fetch filter capabilities catalog from the server. */
export async function fetchFilterCapabilities(): Promise<FilterCapability[]> {
  const data = await apiFetch<{ filters: FilterCapability[] }>("/filters/capabilities");
  return data.filters;
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
}

export interface PersonFacesResponse {
  items: PersonFaceItem[];
  next_cursor: string | null;
}

export interface ClusterItem {
  cluster_index: number;
  size: number;
  faces: PersonFaceItem[];
}

export interface ClustersResponse {
  clusters: ClusterItem[];
  truncated: boolean;
  max_cluster_size: number;
}

/** List people sorted by face count descending. */
export async function listPeople(cursor?: string, limit = 50): Promise<PersonListResponse> {
  const params = new URLSearchParams();
  if (cursor) params.set("after", cursor);
  params.set("limit", String(limit));
  return apiFetch<PersonListResponse>(`/people?${params}`);
}

/** Create a named person. */
export async function createPerson(displayName: string, faceIds?: string[]): Promise<PersonItem> {
  const body: Record<string, unknown> = { display_name: displayName };
  if (faceIds) body.face_ids = faceIds;
  return apiFetch<PersonItem>("/people", { method: "POST", body });
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
export async function getClusters(limit = 20, facesPerCluster = 6, minClusterSize = 2): Promise<ClustersResponse> {
  const params = new URLSearchParams({
    limit: String(limit),
    faces_per_cluster: String(facesPerCluster),
    min_cluster_size: String(minClusterSize),
  });
  return apiFetch<ClustersResponse>(`/faces/clusters?${params}`);
}

/** Name a cluster: creates a new person or assigns to existing, for ALL faces in the cluster. */
export async function nameCluster(
  clusterIndex: number,
  opts: { displayName: string } | { personId: string; displayName: string },
): Promise<PersonItem> {
  const body: Record<string, unknown> = { display_name: opts.displayName };
  if ("personId" in opts) body.person_id = opts.personId;
  return apiFetch<PersonItem>(`/faces/clusters/${clusterIndex}/name`, {
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
export async function getNearestPeople(clusterIndex: number, limit = 5): Promise<NearestPersonItem[]> {
  return apiFetch<NearestPersonItem[]>(`/faces/clusters/${clusterIndex}/nearest-people?limit=${limit}`);
}

/** Get people sorted by similarity to a single face's embedding. Used by
 * the lightbox face-assignment popover so the candidate dropdown is
 * ordered by who actually looks like the clicked face, instead of by
 * total face count (which has nothing to do with whether they match). */
export async function getNearestPeopleForFace(faceId: string, limit = 5): Promise<NearestPersonItem[]> {
  return apiFetch<NearestPersonItem[]>(`/faces/${faceId}/nearest-people?limit=${limit}`);
}

/** Dismiss a cluster: creates a dismissed person that absorbs future similar faces. Returns person_id for undo. */
export async function dismissCluster(clusterIndex: number): Promise<{ person_id: string }> {
  const res = await fetch(`/v1/faces/clusters/${clusterIndex}/dismiss`, {
    method: "POST",
    headers: authHeaders(),
  });
  return res.json();
}

export interface ClusterFacesResponse {
  items: PersonFaceItem[];
  total: number;
  next_cursor: string | null;
}

/** List all faces in a cluster, paginated. */
export async function listClusterFaces(clusterIndex: number, cursor?: string, limit = 50): Promise<ClusterFacesResponse> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor) params.set("after", cursor);
  return apiFetch<ClusterFacesResponse>(`/faces/clusters/${clusterIndex}/faces?${params}`);
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

/** Correct a clip's description, OCR or tags (editors). Only the fields sent change:
 * a string sets the correction, null brings back the machine's; `tags` is the list to
 * show, kept as adds and removes on the machine's. Returns the clip's detail. */
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
  source: "analysis_proxy" | "preview";
  /** Seconds the link plays; null means the whole video. */
  max_seconds: number | null;
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
}

export async function getTenantSettings(): Promise<TenantSettings> {
  return apiFetch<TenantSettings>("/tenant/settings");
}

/** Change account settings (admins). Fewer trash_days answers 409
 * trash_days_shortened (with counts) when it would delete things at once,
 * until confirm_purge says yes. */
export async function updateTenantSettings(
  update: Partial<TenantSettings> & { confirm_purge?: boolean },
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
  jobs: string[];
  /** Requests it takes at once. */
  at_once: number;
  enabled: boolean;
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
}

/** Add a machine (admins); it's asked for its models. 502 machine_unreachable,
 * 409 model_not_offered (details: job, model, models), 409 name_taken. */
export async function addMachine(body: MachineFields): Promise<AiSettings> {
  return apiFetch<AiSettings>("/ai/machines", { method: "POST", body });
}

/** Change a machine (admins): only the fields sent. 409
 * job_left_without_machine (details.jobs) unless leaveJobs, when it's the last
 * machine doing a job; otherwise as addMachine. */
export async function updateMachine(machineId: string, body: Partial<MachineFields>, leaveJobs = false): Promise<AiSettings> {
  return apiFetch<AiSettings>(`/ai/machines/${machineId}${leaveJobs ? "?leave_jobs=true" : ""}`, { method: "PATCH", body });
}

/** Remove a machine (admins). 409 job_left_without_machine unless leaveJobs. */
export async function removeMachine(machineId: string, leaveJobs = false): Promise<AiSettings> {
  return apiFetch<AiSettings>(`/ai/machines/${machineId}${leaveJobs ? "?leave_jobs=true" : ""}`, { method: "DELETE" });
}

/** Set a job's model (admins); "" turns the job off. Every machine doing it is
 * asked: 409 model_not_offered (details.machines: name, models, error) when none offers it.
 * 409 upgrades_stop (details.upgrades: artifact, title, remaining) when upgrades to the
 * current model are under way, unless stopUpgrades. */
export async function setJobModel(job: string, model: string, stopUpgrades = false): Promise<AiSettings> {
  return apiFetch<AiSettings>(`/ai/jobs/${job}`, {
    method: "PUT",
    body: stopUpgrades ? { model, stop_upgrades: true } : { model },
  });
}

/** A producer's artifacts over the clips in sight (in one library or project when asked). */
export interface ProducerCounts {
  applicable: number;
  current: number;
  /** Made with an older producer or settings. */
  stale: number;
  /** Not made yet. */
  missing: number;
  /** The last try failed. */
  failing: number;
}

export interface UpgradeScope {
  kind: "all" | "library" | "project";
  id: string | null;
  name: string | null;
}

export type EditsChoice = "keep" | "replace" | "skip";

/** Stale artifacts an admin approved making again: the clips stale in its scope then. */
export interface ProducerUpgrade {
  upgrade_id: string;
  scope: UpgradeScope;
  edits: EditsChoice;
  approved_by: string | null;
  approved_at: string;
  total: number;
  remaining: number;
  /** Made again since, but still not what it upgrades to: stale, not handed out again. */
  still_stale?: number;
}

/** What makes one kind of artifact (Settings → Processing). */
export interface Producer {
  artifact: string;
  producer: string;
  version: string;
  title: string;
  media: string[];
  /** Its output must come from one model across the library: upgraded all at once. */
  uniform: boolean;
  settings: Record<string, unknown>;
  settings_hash: string;
  counts: ProducerCounts | null;
  upgradable: boolean;
  why_not: string | null;
  /** Stale clips (in the counts' scope) with a person's edits on top. */
  edited: number;
  upgrades: ProducerUpgrade[];
}

export async function getProducers(scope: { libraryId?: string; projectId?: string } = {}): Promise<Producer[]> {
  const qs = new URLSearchParams();
  if (scope.libraryId) qs.set("library_id", scope.libraryId);
  if (scope.projectId) qs.set("project_id", scope.projectId);
  const q = qs.toString();
  return (await apiFetch<{ producers: Producer[] }>(`/producers${q ? `?${q}` : ""}`)).producers;
}

export interface UpgradeRequest {
  library_id?: string;
  project_id?: string;
  edits?: EditsChoice;
  confirm?: boolean;
}

export interface UpgradeResult {
  upgrade_id: string | null;
  artifact: string;
  scope: UpgradeScope;
  edits: EditsChoice;
  upgrading: number;
  skipped_edited: number;
  /** edits=replace: clips whose edits move to history as each is made again. */
  edits_to_replace: number;
}

/** Approve making a producer's stale artifacts again (admins). 409
 * edited_clips (details: stale, edited, choices) until `edits` says what to do
 * with clips a person edited; 409 redo_everything (details: stale) until
 * `confirm`, for producers upgraded all at once; 409 nothing_stale /
 * cant_upgrade; 422 all_or_nothing when such a producer is narrowed. */
export async function upgradeProducer(artifact: string, body: UpgradeRequest): Promise<UpgradeResult> {
  return apiFetch<UpgradeResult>(`/producers/${artifact}/upgrade`, { method: "POST", body });
}

/** Stop an upgrade, or all of a producer's (admins): what isn't made again stays stale. */
export async function cancelUpgrade(artifact: string, upgradeId?: string): Promise<void> {
  const q = upgradeId ? `?upgrade_id=${encodeURIComponent(upgradeId)}` : "";
  await apiFetch<void>(`/producers/${artifact}/upgrade${q}`, { method: "DELETE" });
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

export function thumbnailUrl(assetId: string): string {
  return `/v1/assets/${assetId}/thumbnail`;
}

export function proxyUrl(assetId: string): string {
  return `/v1/assets/${assetId}/proxy`;
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

export interface TenantFilterDefaultItem {
  default_id: string;
  pattern: string;
  created_at: string;
}

export interface TenantFilterDefaultsResponse {
  includes: TenantFilterDefaultItem[];
  excludes: TenantFilterDefaultItem[];
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

export interface CreatedTenantDefaultResponse {
  default_id: string;
  type: string;
  pattern: string;
  created_at: string;
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

export async function getTenantFilterDefaults(): Promise<TenantFilterDefaultsResponse> {
  return apiFetch<TenantFilterDefaultsResponse>("/path-filter-defaults");
}

export async function addTenantFilterDefault(
  type: "include" | "exclude",
  pattern: string,
): Promise<CreatedTenantDefaultResponse> {
  return apiFetch<CreatedTenantDefaultResponse>("/path-filter-defaults", {
    method: "POST",
    body: { type, pattern },
  });
}

export async function deleteTenantFilterDefault(
  defaultId: string,
): Promise<void> {
  return apiFetch<void>(`/path-filter-defaults/${defaultId}`, {
    method: "DELETE",
  });
}

export async function listUsers(): Promise<UserItem[]> {
  return apiFetch<UserItem[]>("/users");
}

export async function createUser(
  email: string,
  password: string,
  role: string,
): Promise<UserItem> {
  return apiFetch<UserItem>("/users", {
    method: "POST",
    body: { email, password, role },
  });
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
    type?: string;
    saved_query?: Record<string, unknown>;
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
    saved_query?: { q?: string; filters: Record<string, unknown>; library_id?: string } | null;
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

/** Delete trashed projects for good: these ones, or the whole trash. Their
 * clips stay in the library. */
export async function emptyProjectTrash(projectIds?: string[]): Promise<{ deleted: number }> {
  return apiFetch<{ deleted: number }>("/projects/empty-trash", {
    method: "POST",
    body: projectIds ? { project_ids: projectIds } : {},
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

export async function reorderProject(
  projectId: string,
  assetIds: string[],
): Promise<void> {
  return apiFetch<void>(`/projects/${projectId}/reorder`, {
    method: "PATCH",
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

export async function listFavorites(
  cursor?: string,
  limit = 200,
): Promise<{ items: (AssetPageItem & { library_id: string; library_name: string })[]; next_cursor: string | null }> {
  const qs = new URLSearchParams();
  if (cursor) qs.set("after", cursor);
  qs.set("limit", String(limit));
  return apiFetch(`/assets/favorites?${qs}`);
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

export async function reorderSavedViews(viewIds: string[]): Promise<void> {
  return apiFetch<void>("/views/reorder", {
    method: "PATCH",
    body: { view_ids: viewIds },
  });
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

/** Remove the transcript a video asset shows: a person's ("manual") or the
 * machine's. 409 transcript_changed when it shows the other one. */
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
  /** Photos in the project that the export left out (video only for now). */
  skippedStills: number;
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

/** Export a project as a bin of master clips for an editor. */
export async function exportProject(
  projectId: string,
  format: string,
  prefix?: string,
): Promise<ProjectExportFile> {
  const qs = new URLSearchParams({ format });
  if (prefix) qs.set("prefix", prefix);
  const url = `/v1/projects/${projectId}/export?${qs.toString()}`;
  let res = await fetch(url, { headers: authHeaders() });
  if (res.status === 401 && (await tryRefresh())) {
    res = await fetch(url, { headers: authHeaders() });
  }
  if (!res.ok) {
    let message = `Export failed (${res.status})`;
    try {
      const body = await res.json();
      message = body?.error?.message ?? body?.detail ?? message;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, message);
  }
  return {
    blob: await res.blob(),
    filename: filenameFromDisposition(res.headers.get("Content-Disposition")) ?? "project-export",
    skippedStills: Number(res.headers.get("X-Lumiverb-Skipped-Stills") ?? 0) || 0,
    skippedNoDuration: Number(res.headers.get("X-Lumiverb-Skipped-No-Duration") ?? 0) || 0,
    unprobed: Number(res.headers.get("X-Lumiverb-Unprobed") ?? 0) || 0,
    skippedTrashed: Number(res.headers.get("X-Lumiverb-Skipped-Trashed") ?? 0) || 0,
    skippedMissing: Number(res.headers.get("X-Lumiverb-Skipped-Missing") ?? 0) || 0,
    skippedLibraryTrashed: Number(res.headers.get("X-Lumiverb-Skipped-Library-Trashed") ?? 0) || 0,
    skippedArchived: Number(res.headers.get("X-Lumiverb-Skipped-Archived") ?? 0) || 0,
  };
}

const DEFAULT_EXPORT_FORMAT_KEY = "lumiverb.defaultExportFormat";
const DEFAULT_EXPORT_PREFIX_KEY = "lumiverb.defaultExportPrefix";

/** The format the Export button uses without asking; null until the user picks one. */
export function getDefaultExportFormat(): string | null {
  try {
    return localStorage.getItem(DEFAULT_EXPORT_FORMAT_KEY);
  } catch {
    return null;
  }
}

/** The media location saved with the default format, if any. */
export function getDefaultExportPrefix(): string | null {
  try {
    return localStorage.getItem(DEFAULT_EXPORT_PREFIX_KEY);
  } catch {
    return null;
  }
}

/** Remember a format (and the media location to use with it) for one-click Export. */
export function setDefaultExportFormat(format: string | null, prefix?: string): void {
  try {
    if (format) localStorage.setItem(DEFAULT_EXPORT_FORMAT_KEY, format);
    else localStorage.removeItem(DEFAULT_EXPORT_FORMAT_KEY);
    if (format && prefix) localStorage.setItem(DEFAULT_EXPORT_PREFIX_KEY, prefix);
    else localStorage.removeItem(DEFAULT_EXPORT_PREFIX_KEY);
  } catch {
    /* storage unavailable: the chooser just opens each time */
  }
}
