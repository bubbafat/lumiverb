import type {
  AssetDetail,
  AssetPageItem,
  DirectoryNode,
  EmptyTrashResponse,
  JobListItem,
  LibraryListItem,
  LibraryResponse,
  SearchResponse,
  SimilarityResponse,
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

const apiKey = import.meta.env.VITE_API_KEY as string | undefined;

const authHeaders = (): HeadersInit =>
  apiKey ? { Authorization: `Bearer ${apiKey}` } : {};

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
    return undefined as T;
  }
  return res.json() as Promise<T>;
}

/** Fetch blob (e.g. image) with auth. Used for thumbnail/proxy URLs. */
export async function apiFetchBlob(path: string): Promise<Blob> {
  const res = await fetch(`/v1${path}`, { headers: authHeaders() });
  if (!res.ok) throw new ApiError(res.status, res.statusText);
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

/** Paginated assets for a library. Returns null on 204 (end of pages). */
export async function pageAssets(
  libraryId: string,
  after?: string,
  limit = 100,
  pathPrefix?: string,
   tag?: string,
): Promise<AssetPageItem[] | null> {
  const params = new URLSearchParams({ library_id: libraryId });
  if (after) params.set("after", after);
  params.set("limit", String(limit));
  if (pathPrefix) params.set("path_prefix", pathPrefix);
  if (tag) params.set("tag", tag);
  const res = await fetch(`/v1/assets/page?${params}`, {
    headers: authHeaders(),
  });
  if (!res.ok) {
    let message = res.statusText;
    try {
      const json = (await res.json()) as { error?: { message?: string } };
      message = json?.error?.message ?? message;
    } catch {
      // ignore
    }
    throw new ApiError(res.status, message);
  }
  if (res.status === 204) return null;
  return res.json() as Promise<AssetPageItem[]>;
}

export async function searchAssets(params: {
  libraryId: string;
  q: string;
  pathPrefix?: string;
  tag?: string;
  limit?: number;
  offset?: number;
}): Promise<SearchResponse> {
  const qs = new URLSearchParams({ library_id: params.libraryId, q: params.q });
  if (params.pathPrefix) qs.set("path_prefix", params.pathPrefix);
  if (params.tag) qs.set("tag", params.tag);
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.offset) qs.set("offset", String(params.offset));
  return apiFetch<SearchResponse>(`/search?${qs.toString()}`);
}

export async function getAsset(assetId: string): Promise<AssetDetail> {
  return apiFetch<AssetDetail>(`/assets/${assetId}`);
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

