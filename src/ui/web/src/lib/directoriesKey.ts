/**
 * The query key for one folder listing; `null` is the library's top level.
 * Invalidating `["directories", libraryId]` refetches every listing of that
 * library that's on screen (the folder tree's top level and open folders,
 * and the current folder's count over the grid).
 */
export function directoriesKey(libraryId: string, parent: string | null) {
  return ["directories", libraryId, parent] as const;
}
