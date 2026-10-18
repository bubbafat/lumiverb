import type { LibraryPerson, LibraryRole } from "../api/types";

/** The caller's role on a library (ADR-018 decision 8): the library says it; a server from before roles per
 * library doesn't, and the account role stands for it. */
export function roleOn(library: { role?: LibraryRole | null } | undefined, accountRole: string | undefined): string | undefined {
  return library?.role ?? accountRole;
}

/** Edits the library's clips: its editors and its admin. */
export function editsIt(role: string | undefined): boolean {
  return role === "admin" || role === "editor";
}

/** Runs the library: its settings, folder, reader, Scans pause, privacy and people. */
export function runsIt(role: string | undefined): boolean {
  return role === "admin";
}

export const ROLE_NAMES: Record<LibraryRole, string> = {
  admin: "Admin",
  editor: "Editor",
  viewer: "Viewer",
  none: "No access",
};

/** What a person's role on a library is when none is set for them: none on a private library or for someone who
 * sees only what they're given; their account role otherwise. */
export function defaultRole(person: LibraryPerson, isPrivate: boolean): LibraryRole {
  if (person.account_role === "admin") return "admin";
  if (isPrivate || !person.every_library) return "none";
  return person.account_role as LibraryRole;
}

/** Why someone has the role they have on a library, in a few words. */
export function howTheyHaveIt(person: LibraryPerson, isPrivate: boolean): string {
  if (person.account_role === "admin") return "Account admin: every library's admin";
  if (person.set_role !== null) return "Set for this library";
  if (isPrivate) return "Private library: given to them only";
  if (!person.every_library) return "Sees only the libraries they're given";
  return "Their account role";
}
