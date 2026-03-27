import type {
  ApiKeyCreateResponse,
  ApiKeyItem,
  AssetDetail,
  AssetPageResponse,
  CurrentUser,
  DirectoryNode,
  EmptyTrashResponse,
  FacetsResponse,
  JobListItem,
  LibraryListItem,
  LibraryResponse,
  LibraryRevision,
  SearchResponse,
  SimilarityResponse,
  UserItem,
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
  const res = await fetch(`/v1${path}`, { headers: authHeaders() });
  if (!res.ok) {
    if (res.status === 401) handleUnauthorized();
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
  apertureMin?: number;
  apertureMax?: number;
  focalLengthMin?: number;
  focalLengthMax?: number;
  hasExposure?: boolean;
  hasGps?: boolean;
  nearLat?: number;
  nearLon?: number;
  nearRadiusKm?: number;
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
  if (opts?.apertureMin != null) params.set("aperture_min", String(opts.apertureMin));
  if (opts?.apertureMax != null) params.set("aperture_max", String(opts.apertureMax));
  if (opts?.focalLengthMin != null) params.set("focal_length_min", String(opts.focalLengthMin));
  if (opts?.focalLengthMax != null) params.set("focal_length_max", String(opts.focalLengthMax));
  if (opts?.hasExposure != null) params.set("has_exposure", String(opts.hasExposure));
  if (opts?.hasGps) params.set("has_gps", "true");
  if (opts?.nearLat != null) params.set("near_lat", String(opts.nearLat));
  if (opts?.nearLon != null) params.set("near_lon", String(opts.nearLon));
  if (opts?.nearRadiusKm != null) params.set("near_radius_km", String(opts.nearRadiusKm));
  return apiFetch<AssetPageResponse>(`/assets/page?${params}`);
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

export async function searchAssets(params: {
  libraryId: string;
  q: string;
  pathPrefix?: string;
  tag?: string;
  dateFrom?: string;
  dateTo?: string;
  limit?: number;
  offset?: number;
}): Promise<SearchResponse> {
  const qs = new URLSearchParams({ library_id: params.libraryId, q: params.q });
  if (params.pathPrefix) qs.set("path_prefix", params.pathPrefix);
  if (params.tag) qs.set("tag", params.tag);
  if (params.dateFrom) qs.set("date_from", params.dateFrom);
  if (params.dateTo) qs.set("date_to", params.dateTo);
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.offset) qs.set("offset", String(params.offset));
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
): Promise<CreatedFilterResponse> {
  return apiFetch<CreatedFilterResponse>(
    `/libraries/${libraryId}/filters`,
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

export async function getJobStats(): Promise<import("./types").JobStatsResponse> {
  return apiFetch<import("./types").JobStatsResponse>("/jobs/stats");
}

export async function listJobs(params: {
  status?: string;
  limit?: number;
  libraryId?: string;
}): Promise<JobListItem[]> {
  const qs = new URLSearchParams();
  if (params.status) qs.set("status", params.status);
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.libraryId) qs.set("library_id", params.libraryId);
  return apiFetch<JobListItem[]>(`/jobs?${qs.toString()}`);
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

