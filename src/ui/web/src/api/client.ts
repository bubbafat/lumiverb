import type { EmptyTrashResponse, LibraryListItem, LibraryResponse } from "./types";

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

async function apiFetch<T>(
  path: string,
  options?: RequestInit & { body?: unknown },
): Promise<T> {
  const { body, ...rest } = options ?? {};
  const headers: HeadersInit = {
    "Content-Type": "application/json",
    ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
    ...rest.headers,
  };
  const res = await fetch(`/v1${path}`, {
    ...rest,
    headers,
    body: body !== undefined ? JSON.stringify(body) : rest.body,
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
