/** Where this account's libraries on this server may be, when a server admin gave it folders (Admin › Accounts):
 * said wherever a library's folder on this server is chosen, as the server holds it to them (403
 * outside_account_folders). Nothing when the account may choose any folder the server reads (null), as on a server
 * with one account. */
export function AccountFoldersNote({ folders }: { folders: string[] | null | undefined }) {
  if (folders == null) return null;
  return (
    <p className="mt-1 text-xs text-gray-500">
      {folders.length === 0 ? (
        "This account has no folders on this server: only a computer of its own (Settings › Devices) reads its libraries."
      ) : (
        <>
          This account&apos;s folders: <span className="break-all font-mono text-gray-400">{folders.join(", ")}</span>
        </>
      )}
    </p>
  );
}
