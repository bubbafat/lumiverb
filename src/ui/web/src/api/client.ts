import type {
  ApiKeyCreateResponse,
  ApiKeyItem,
  AssetDetail,
  AssetPageItem,
  AssetPageResponse,
  BatchAddResponse,
  BatchRemoveResponse,
  CollectionAssetsResponse,
  CollectionItem,
  CollectionListResponse,
  CurrentUser,
  DirectoryNode,
  EmptyTrashResponse,
  FaceListResponse,
  FacetsResponse,
  LibraryListItem,
  LibraryResponse,
  LibraryRevision,
  SearchResponse,
  SimilarityResponse,
  UserItem,
  RatingResponse,
  RatingLookupResponse,
  BatchRatingResponse,
  BrowseResponse,
} from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
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
    try {
      const json = (await res.json()) as { error?: { message?: string } };
      message = json?.error?.message ?? message;
    } catch {
      // ignore
    }
    throw new ApiError(res.status, message);
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

export async function deleteLibrary(libraryId: string): Promise<void> {
  return apiFetch<void>(`/libraries/${libraryId}`, { method: "DELETE" });
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

export async function emptyTrash(): Promise<EmptyTrashResponse> {
  return apiFetch<EmptyTrashResponse>("/libraries/empty-trash", {
    method: "POST",
  });
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

export interface PageAssetsOptions {
  pathPrefix?: string;
  tag?: string;
  sort?: string;
  dir?: "asc" | "desc";
  mediaType?: string;
  cameraMake?: string;
  cameraModel?: string;
  lensModel?: string;
  isoMin?: number;
  isoMax?: number;
  exposureMinUs?: number;
  exposureMaxUs?: number;
  apertureMin?: number;
  apertureMax?: number;
  focalLengthMin?: number;
  focalLengthMax?: number;
  hasExposure?: boolean;
  hasGps?: boolean;
  hasFaces?: boolean;
  personId?: string;
  nearLat?: number;
  nearLon?: number;
  nearRadiusKm?: number;
  favorite?: boolean;
  starMin?: number;
  starMax?: number;
  color?: string;
  hasRating?: boolean;
}

/** Paginated assets for a library with sort/filter support. */
export async function pageAssets(
  libraryId: string,
  cursor?: string,
  limit = 100,
  opts?: PageAssetsOptions,
): Promise<AssetPageResponse> {
  const params = new URLSearchParams({ library_id: libraryId });
  if (cursor) params.set("after", cursor);
  params.set("limit", String(limit));
  if (opts?.pathPrefix) params.set("path_prefix", opts.pathPrefix);
  if (opts?.tag) params.set("tag", opts.tag);
  if (opts?.sort) params.set("sort", opts.sort);
  if (opts?.dir) params.set("dir", opts.dir);
  if (opts?.mediaType) params.set("media_type", opts.mediaType);
  if (opts?.cameraMake) params.set("camera_make", opts.cameraMake);
  if (opts?.cameraModel) params.set("camera_model", opts.cameraModel);
  if (opts?.lensModel) params.set("lens_model", opts.lensModel);
  if (opts?.isoMin != null) params.set("iso_min", String(opts.isoMin));
  if (opts?.isoMax != null) params.set("iso_max", String(opts.isoMax));
  if (opts?.exposureMinUs != null) params.set("exposure_min_us", String(opts.exposureMinUs));
  if (opts?.exposureMaxUs != null) params.set("exposure_max_us", String(opts.exposureMaxUs));
  if (opts?.apertureMin != null) params.set("aperture_min", String(opts.apertureMin));
  if (opts?.apertureMax != null) params.set("aperture_max", String(opts.apertureMax));
  if (opts?.focalLengthMin != null) params.set("focal_length_min", String(opts.focalLengthMin));
  if (opts?.focalLengthMax != null) params.set("focal_length_max", String(opts.focalLengthMax));
  if (opts?.hasExposure != null) params.set("has_exposure", String(opts.hasExposure));
  if (opts?.hasGps) params.set("has_gps", "true");
  if (opts?.hasFaces) params.set("has_faces", "true");
  if (opts?.personId) params.set("person_id", opts.personId);
  if (opts?.nearLat != null) params.set("near_lat", String(opts.nearLat));
  if (opts?.nearLon != null) params.set("near_lon", String(opts.nearLon));
  if (opts?.nearRadiusKm != null) params.set("near_radius_km", String(opts.nearRadiusKm));
  if (opts?.favorite != null) params.set("favorite", String(opts.favorite));
  if (opts?.starMin != null) params.set("star_min", String(opts.starMin));
  if (opts?.starMax != null) params.set("star_max", String(opts.starMax));
  if (opts?.color) params.set("color", opts.color);
  if (opts?.hasRating != null) params.set("has_rating", String(opts.hasRating));
  return apiFetch<AssetPageResponse>(`/assets/page?${params}`);
}

/** Cross-library paginated browse with full filter support. */
export async function browseAll(
  cursor?: string,
  limit = 100,
  opts?: PageAssetsOptions & { libraryId?: string },
): Promise<BrowseResponse> {
  const params = new URLSearchParams();
  if (cursor) params.set("after", cursor);
  params.set("limit", String(limit));
  if (opts?.libraryId) params.set("library_id", opts.libraryId);
  if (opts?.pathPrefix) params.set("path_prefix", opts.pathPrefix);
  if (opts?.tag) params.set("tag", opts.tag);
  if (opts?.sort) params.set("sort", opts.sort);
  if (opts?.dir) params.set("dir", opts.dir);
  if (opts?.mediaType) params.set("media_type", opts.mediaType);
  if (opts?.cameraMake) params.set("camera_make", opts.cameraMake);
  if (opts?.cameraModel) params.set("camera_model", opts.cameraModel);
  if (opts?.lensModel) params.set("lens_model", opts.lensModel);
  if (opts?.isoMin != null) params.set("iso_min", String(opts.isoMin));
  if (opts?.isoMax != null) params.set("iso_max", String(opts.isoMax));
  if (opts?.exposureMinUs != null) params.set("exposure_min_us", String(opts.exposureMinUs));
  if (opts?.exposureMaxUs != null) params.set("exposure_max_us", String(opts.exposureMaxUs));
  if (opts?.apertureMin != null) params.set("aperture_min", String(opts.apertureMin));
  if (opts?.apertureMax != null) params.set("aperture_max", String(opts.apertureMax));
  if (opts?.focalLengthMin != null) params.set("focal_length_min", String(opts.focalLengthMin));
  if (opts?.focalLengthMax != null) params.set("focal_length_max", String(opts.focalLengthMax));
  if (opts?.hasExposure != null) params.set("has_exposure", String(opts.hasExposure));
  if (opts?.hasGps) params.set("has_gps", "true");
  if (opts?.hasFaces) params.set("has_faces", "true");
  if (opts?.personId) params.set("person_id", opts.personId);
  if (opts?.nearLat != null) params.set("near_lat", String(opts.nearLat));
  if (opts?.nearLon != null) params.set("near_lon", String(opts.nearLon));
  if (opts?.nearRadiusKm != null) params.set("near_radius_km", String(opts.nearRadiusKm));
  if (opts?.favorite != null) params.set("favorite", String(opts.favorite));
  if (opts?.starMin != null) params.set("star_min", String(opts.starMin));
  if (opts?.starMax != null) params.set("star_max", String(opts.starMax));
  if (opts?.color) params.set("color", opts.color);
  if (opts?.hasRating != null) params.set("has_rating", String(opts.hasRating));
  return apiFetch<BrowseResponse>(`/browse?${params}`);
}

/** Fetch aggregated filter facets for a library. */
export async function getFacets(
  libraryId: string,
  pathPrefix?: string,
): Promise<FacetsResponse> {
  const params = new URLSearchParams({ library_id: libraryId });
  if (pathPrefix) params.set("path_prefix", pathPrefix);
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
export async function getClusters(limit = 20, facesPerCluster = 6): Promise<ClustersResponse> {
  const params = new URLSearchParams({ limit: String(limit), faces_per_cluster: String(facesPerCluster) });
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

export async function searchAssets(params: {
  libraryId?: string;
  q: string;
  pathPrefix?: string;
  tag?: string;
  dateFrom?: string;
  dateTo?: string;
  limit?: number;
  offset?: number;
  favorite?: boolean;
  starMin?: number;
  starMax?: number;
  color?: string;
  hasRating?: boolean;
  hasFaces?: boolean;
  personId?: string;
}): Promise<SearchResponse> {
  const qs = new URLSearchParams({ q: params.q });
  if (params.libraryId) qs.set("library_id", params.libraryId);
  if (params.pathPrefix) qs.set("path_prefix", params.pathPrefix);
  if (params.tag) qs.set("tag", params.tag);
  if (params.dateFrom) qs.set("date_from", params.dateFrom);
  if (params.dateTo) qs.set("date_to", params.dateTo);
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.offset) qs.set("offset", String(params.offset));
  if (params.favorite != null) qs.set("favorite", String(params.favorite));
  if (params.starMin != null) qs.set("star_min", String(params.starMin));
  if (params.starMax != null) qs.set("star_max", String(params.starMax));
  if (params.color) qs.set("color", params.color);
  if (params.hasRating != null) qs.set("has_rating", String(params.hasRating));
  if (params.hasFaces) qs.set("has_faces", "true");
  if (params.personId) qs.set("person_id", params.personId);
  return apiFetch<SearchResponse>(`/search?${qs.toString()}`);
}

export async function getAsset(assetId: string, publicLibraryId?: string): Promise<AssetDetail> {
  const qs = publicLibraryId
    ? `?public_library_id=${encodeURIComponent(publicLibraryId)}`
    : "";
  return apiFetch<AssetDetail>(`/assets/${assetId}${qs}`);
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

export async function addLibraryFilter(
  libraryId: string,
  type: "include" | "exclude",
  pattern: string,
  trashMatching = false,
): Promise<CreatedFilterResponse> {
  return apiFetch<CreatedFilterResponse>(
    `/libraries/${libraryId}/filters`,
    { method: "POST", body: { type, pattern, trash_matching: trashMatching } },
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
// Collections
// ---------------------------------------------------------------------------

export async function listCollections(): Promise<CollectionItem[]> {
  const res = await apiFetch<CollectionListResponse>("/collections");
  return res.items;
}

export async function getCollection(
  collectionId: string,
): Promise<CollectionItem> {
  return apiFetch<CollectionItem>(`/collections/${collectionId}`);
}

export async function createCollection(
  name: string,
  opts?: { description?: string; sort_order?: string; visibility?: string; asset_ids?: string[] },
): Promise<CollectionItem> {
  return apiFetch<CollectionItem>("/collections", {
    method: "POST",
    body: { name, ...opts },
  });
}

export async function updateCollection(
  collectionId: string,
  body: {
    name?: string;
    description?: string | null;
    visibility?: string;
    sort_order?: string;
    cover_asset_id?: string | null;
  },
): Promise<CollectionItem> {
  return apiFetch<CollectionItem>(`/collections/${collectionId}`, {
    method: "PATCH",
    body,
  });
}

export async function deleteCollection(
  collectionId: string,
): Promise<void> {
  return apiFetch<void>(`/collections/${collectionId}`, { method: "DELETE" });
}

export async function listCollectionAssets(
  collectionId: string,
  after?: string,
  limit = 200,
): Promise<CollectionAssetsResponse> {
  const qs = new URLSearchParams();
  if (after) qs.set("after", after);
  qs.set("limit", String(limit));
  return apiFetch<CollectionAssetsResponse>(
    `/collections/${collectionId}/assets?${qs.toString()}`,
  );
}

export async function addAssetsToCollection(
  collectionId: string,
  assetIds: string[],
): Promise<BatchAddResponse> {
  return apiFetch<BatchAddResponse>(`/collections/${collectionId}/assets`, {
    method: "POST",
    body: { asset_ids: assetIds },
  });
}

export async function removeAssetsFromCollection(
  collectionId: string,
  assetIds: string[],
): Promise<BatchRemoveResponse> {
  return apiFetch<BatchRemoveResponse>(`/collections/${collectionId}/assets`, {
    method: "DELETE",
    body: { asset_ids: assetIds },
  });
}

export async function reorderCollection(
  collectionId: string,
  assetIds: string[],
): Promise<void> {
  return apiFetch<void>(`/collections/${collectionId}/reorder`, {
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

