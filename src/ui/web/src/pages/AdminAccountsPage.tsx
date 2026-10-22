import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  addServerAdmin,
  createAccount,
  deleteAccount,
  getCurrentUser,
  getNetworkAccess,
  listAccounts,
  listServerAdmins,
  removeServerAdmin,
  renameAccount,
  setAccountFolders,
} from "../api/client";
import { decodeJwtClaims } from "../api/jwt";
import type { AccountItem } from "../api/types";
import { InviteLinkPanel, type MadeLink } from "./AdminUsersPage";

const ACCOUNTS = ["admin", "accounts"];
const SERVER_ADMINS = ["admin", "server-admins"];
const input = "w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-100 placeholder-gray-500 outline-hidden focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500";
const button = "rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed";

function said(error: unknown, otherwise: string): string {
  return error instanceof ApiError ? error.message : otherwise;
}

function day(at: string | null): string {
  return at ? new Date(at).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "";
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}

/** The install's accounts and its server admins: a server admin's (ADR-018 decision 8). An account is made with
 * its first admin invited, renamed, given the folders on this server its libraries may be in, or deleted once what
 * it holds is said and its name typed: nobody signs in to a deleted account again. */
export default function AdminAccountsPage() {
  const { data: user } = useQuery({ queryKey: ["settings", "me"], queryFn: getCurrentUser });
  if (user && !user.server_admin) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-6">
        <h1 className="text-2xl font-semibold text-gray-100">Accounts</h1>
        <p className="mt-2 text-sm text-gray-400">Only a server admin manages the accounts on this server.</p>
      </div>
    );
  }
  return (
    <div className="mx-auto max-w-3xl px-6 py-6 space-y-10">
      <div>
        <h1 className="text-2xl font-semibold text-gray-100">Accounts</h1>
        <p className="mt-1 text-sm text-gray-500">
          Each account has its own libraries, people and settings. <Link to="/admin" className="text-indigo-400 hover:text-indigo-300">Admin</Link>
        </p>
      </div>
      <Accounts />
      <ServerAdmins />
    </div>
  );
}

function Accounts() {
  const qc = useQueryClient();
  const own = decodeJwtClaims()?.tenant_id ?? null;
  const { data: accounts, isLoading, error } = useQuery({ queryKey: ACCOUNTS, queryFn: listAccounts });
  const { data: network } = useQuery({ queryKey: ["system", "network"], queryFn: getNetworkAccess });
  const thisMacOnly = !!network?.available && !network.other_devices;
  const [made, setMade] = useState<MadeLink | null>(null);
  const live = (accounts ?? []).filter((a) => a.status !== "deleted");
  const deleted = (accounts ?? []).filter((a) => a.status === "deleted");
  // With one account there's nobody else's folder to reach: nothing is said.
  const unbounded = live.length > 1 ? live.filter((a) => a.folders == null) : [];

  return (
    <>
      {made && <InviteLinkPanel link={made} thisMacOnly={thisMacOnly} onDone={() => setMade(null)} />}
      <section className="space-y-3">
        {error && <p role="alert" className="text-sm text-red-400">{said(error, "Couldn't list the accounts.")}</p>}
        {unbounded.length > 0 && (
          <p role="note" className="rounded-lg border border-amber-500/40 bg-amber-950/20 px-3 py-2 text-sm text-amber-100/90">
            Each account without folders of its own ({unbounded.map((a) => a.name).join(", ")}) can choose any folder
            the server reads for a library, the other accounts&apos; too. Give each its folders below.
          </p>
        )}
        {isLoading ? (
          <div className="h-24 rounded-lg border border-gray-700/50 bg-gray-900/50 animate-pulse" />
        ) : (
          <ul aria-label="Accounts" className="divide-y divide-gray-800 rounded-lg border border-gray-700/50 bg-gray-900/50">
            {live.map((a) => (
              <AccountRow key={a.tenant_id} account={a} own={a.tenant_id === own}
                          onChanged={() => qc.invalidateQueries({ queryKey: ACCOUNTS })} />
            ))}
          </ul>
        )}
        {deleted.length > 0 && (
          <p className="text-xs text-gray-500">
            Deleted, kept on the server and never signed in to: {deleted.map((a) => a.name).join(", ")}.
          </p>
        )}
      </section>
      <section>
        <h2 className="mb-1 text-lg font-semibold text-gray-100">New account</h2>
        <p className="mb-4 text-sm text-gray-500">
          Its first admin gets a link to send them, and chooses their password there.
        </p>
        <NewAccount onMade={(link) => {
          qc.invalidateQueries({ queryKey: ACCOUNTS });
          setMade(link);
        }} />
      </section>
    </>
  );
}

function NewAccount({ onMade }: { onMade: (link: MadeLink | null) => void }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const create = useMutation({
    mutationFn: () => createAccount(name.trim(), email.trim()),
    onSuccess: (made) => {
      onMade(made.invite_urls?.length ? { email: email.trim(), urls: made.invite_urls } : null);
      setName("");
      setEmail("");
    },
  });
  return (
    <form className="max-w-sm space-y-3" onSubmit={(e) => {
      e.preventDefault();
      create.mutate();
    }}>
      <div>
        <label htmlFor="account-name" className="mb-1 block text-sm font-medium text-gray-300">Name</label>
        <input id="account-name" value={name} maxLength={100} onChange={(e) => setName(e.target.value)}
               placeholder="Ana's photos" required className={input} />
      </div>
      <div>
        <label htmlFor="account-email" className="mb-1 block text-sm font-medium text-gray-300">Its first admin's email</label>
        <input id="account-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)}
               placeholder="ana@example.com" required className={input} />
      </div>
      {create.error && (
        <p role="alert" className="text-sm text-red-400">
          {create.error instanceof ApiError && create.error.code === "email_conflict"
            ? "Someone signs in with that email already."
            : said(create.error, "Couldn't make the account. Try again.")}
        </p>
      )}
      <button type="submit" disabled={create.isPending || !name.trim() || !email.trim()} className={button}>
        {create.isPending ? "One moment…" : "Make the account"}
      </button>
    </form>
  );
}

type Held = { name: string; users: number; libraries: number | null; clips: number | null };

function AccountRow({ account, own, onChanged }: { account: AccountItem; own: boolean; onChanged: () => void }) {
  const [renaming, setRenaming] = useState(false);
  const [newName, setNewName] = useState(account.name);
  const [held, setHeld] = useState<Held | null>(null);
  const [typed, setTyped] = useState("");
  const rename = useMutation({
    mutationFn: () => renameAccount(account.tenant_id, newName.trim()),
    onSuccess: () => {
      setRenaming(false);
      onChanged();
    },
  });
  // Asked without its name first: the server says what it holds (409 confirm_delete_account), shown before anything goes.
  const remove = useMutation({
    mutationFn: (confirm?: string) => deleteAccount(account.tenant_id, confirm),
    onSuccess: () => {
      setHeld(null);
      onChanged();
    },
    onError: (e) => {
      if (e instanceof ApiError && e.code === "confirm_delete_account" && e.details) {
        setHeld(e.details as unknown as Held);
        remove.reset();
      }
    },
  });
  const holds = held && [
    plural(held.users, "person", "people"),
    held.libraries === null ? null : plural(held.libraries, "library", "libraries"),
    held.clips === null ? null : plural(held.clips, "clip"),
  ].filter(Boolean).join(", ");

  return (
    <li aria-label={account.name} className="space-y-2 px-4 py-3">
      {renaming ? (
        <form className="flex flex-wrap gap-2" onSubmit={(e) => {
          e.preventDefault();
          if (newName.trim()) rename.mutate();
        }}>
          <input aria-label="New name" value={newName} maxLength={100} onChange={(e) => setNewName(e.target.value)}
                 className={`${input} max-w-xs`} />
          <button type="submit" disabled={rename.isPending || !newName.trim()} className={button}>Save</button>
          <button type="button" onClick={() => setRenaming(false)} className="text-sm text-gray-400">Cancel</button>
        </form>
      ) : (
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <p className="text-sm text-gray-400">
            <span className="font-medium text-gray-100">{account.name}</span>
            {own && <span className="text-gray-500"> (yours)</span>} · {plural(account.users, "person", "people")}
            {account.created_at && <> · made {day(account.created_at)}</>}
          </p>
          <div className="flex gap-3 text-sm">
            <button type="button" onClick={() => setRenaming(true)} className="text-indigo-400 hover:text-indigo-300">Rename</button>
            {!own && !held && (
              <button type="button" disabled={remove.isPending} onClick={() => remove.mutate(undefined)}
                      className="text-red-300 hover:text-red-200">
                Delete
              </button>
            )}
          </div>
        </div>
      )}
      <Folders account={account} onChanged={onChanged} />
      {held && (
        <form role="alertdialog" aria-label="Delete this account?" className="space-y-2 rounded-lg border border-red-500/40 bg-red-950/20 p-3"
              onSubmit={(e) => {
                e.preventDefault();
                remove.mutate(typed);
              }}>
          <p className="text-sm text-gray-200">
            {held.name} holds {holds}{held.libraries === null && " (what else it holds can't be read now)"}. Deleted,
            nobody signs in to it again; it's kept on the server. Type its name to delete it.
          </p>
          <input aria-label="Its name" value={typed} onChange={(e) => setTyped(e.target.value)} className={`${input} max-w-xs`} />
          <div className="flex gap-2">
            <button type="submit" disabled={typed !== held.name || remove.isPending}
                    className="rounded-lg bg-red-600 px-4 py-2 text-sm font-semibold text-white hover:bg-red-500 disabled:opacity-50">
              Delete {held.name}
            </button>
            <button type="button" onClick={() => {
              setHeld(null);
              setTyped("");
            }} className="text-sm text-gray-400">Cancel</button>
          </div>
        </form>
      )}
      {(rename.error || remove.error) && (
        <p role="alert" className="text-sm text-red-400">{said(rename.error ?? remove.error, "Try again.")}</p>
      )}
    </li>
  );
}

/** The folders on this server the account's libraries may be in (null: every folder the server reads), one per
 * line; what the server refuses (another account's folder, a library left outside) said as it says it. */
function Folders({ account, onChanged }: { account: AccountItem; onChanged: () => void }) {
  const folders = account.folders ?? null;
  const [editing, setEditing] = useState(false);
  const [every, setEvery] = useState(folders === null);
  const [typed, setTyped] = useState((folders ?? []).join("\n"));
  const save = useMutation({
    mutationFn: () => setAccountFolders(account.tenant_id, every ? null : typed.split("\n").map((f) => f.trim()).filter(Boolean)),
    onSuccess: () => {
      setEditing(false);
      onChanged();
    },
  });
  const shown = folders === null
    ? "Every folder the server reads"
    : folders.length === 0 ? "None on this server: only its own computers read its libraries" : folders.join(", ");

  if (!editing) {
    return (
      <p className="flex flex-wrap items-baseline gap-x-3 text-sm text-gray-400">
        <span>Folders: <span className={folders?.length ? "break-all font-mono text-gray-300" : "text-gray-300"}>{shown}</span></span>
        <button type="button" onClick={() => {
          setEvery(folders === null);
          setTyped((folders ?? []).join("\n"));
          save.reset();
          setEditing(true);
        }} className="text-indigo-400 hover:text-indigo-300">
          Change folders
        </button>
      </p>
    );
  }
  return (
    <form aria-label={`Folders for ${account.name}`} className="space-y-2 rounded-lg border border-gray-700/60 p-3" onSubmit={(e) => {
      e.preventDefault();
      save.mutate();
    }}>
      <p className="text-sm text-gray-300">Where its libraries this server reads may be:</p>
      <label className="flex items-center gap-2 text-sm text-gray-200">
        <input type="radio" name={`folders-${account.tenant_id}`} checked={every} onChange={() => setEvery(true)} />
        Every folder the server reads
      </label>
      <label className="flex items-center gap-2 text-sm text-gray-200">
        <input type="radio" name={`folders-${account.tenant_id}`} checked={!every} onChange={() => setEvery(false)} />
        Only these folders
      </label>
      <textarea aria-label="Its folders, one per line" value={typed} disabled={every} rows={3}
                onChange={(e) => setTyped(e.target.value)} placeholder={"/srv/media/ana\n/Volumes/Ana"}
                className={`${input} font-mono disabled:opacity-50`} />
      <p className="text-xs text-gray-500">
        One full path per line. A library this server reads for it must be in one of them; its own computers
        (Settings › Devices) read theirs anywhere.
      </p>
      {save.error && <p role="alert" className="text-sm text-red-400">{said(save.error, "Try again.")}</p>}
      <div className="flex gap-2">
        <button type="submit" disabled={save.isPending} className={button}>Save folders</button>
        <button type="button" onClick={() => setEditing(false)} className="text-sm text-gray-400">Cancel</button>
      </div>
    </form>
  );
}

function ServerAdmins() {
  const qc = useQueryClient();
  const { data: admins, error } = useQuery({ queryKey: SERVER_ADMINS, queryFn: listServerAdmins });
  const [email, setEmail] = useState("");
  const [ended, setEnded] = useState<{ email: string; links: number } | null>(null);
  const add = useMutation({
    mutationFn: () => addServerAdmin(email.trim()),
    onMutate: () => setEnded(null),
    onSuccess: (named) => {
      setEnded(named.links_ended ? { email: email.trim(), links: named.links_ended } : null);
      setEmail("");
      return qc.invalidateQueries({ queryKey: SERVER_ADMINS });
    },
  });
  const remove = useMutation({
    mutationFn: (userId: string) => removeServerAdmin(userId),
    onSuccess: () => qc.invalidateQueries({ queryKey: SERVER_ADMINS }),
  });
  return (
    <section className="space-y-3">
      <div>
        <h2 className="mb-1 text-lg font-semibold text-gray-100">Server admins</h2>
        <p className="text-sm text-gray-500">
          They manage what&apos;s the whole server&apos;s: these accounts, where previews live, the network, backups and the log.
        </p>
      </div>
      {error && <p role="alert" className="text-sm text-red-400">{said(error, "Couldn't list them.")}</p>}
      <ul aria-label="Server admins" className="divide-y divide-gray-800 rounded-lg border border-gray-700/50 bg-gray-900/50">
        {(admins ?? []).map((a) => (
          <li key={a.user_id} aria-label={a.email} className="flex items-baseline justify-between gap-2 px-4 py-2 text-sm">
            <span className="text-gray-200">{a.email}<span className="text-gray-500"> · {a.account}</span></span>
            <button type="button" disabled={remove.isPending} onClick={() => remove.mutate(a.user_id)}
                    className="text-red-300 hover:text-red-200">Remove</button>
          </li>
        ))}
      </ul>
      {remove.error && (
        <p role="alert" className="text-sm text-red-400">
          {remove.error instanceof ApiError && remove.error.code === "last_server_admin"
            ? "They're the last server admin: name another first."
            : said(remove.error, "Try again.")}
        </p>
      )}
      <form className="flex max-w-sm flex-wrap gap-2" onSubmit={(e) => {
        e.preventDefault();
        add.mutate();
      }}>
        <input aria-label="Email of someone who signs in here" type="email" value={email}
               onChange={(e) => setEmail(e.target.value)} placeholder="them@example.com" required className={`${input} min-w-0 flex-1`} />
        <button type="submit" disabled={add.isPending || !email.trim()} className={button}>Make server admin</button>
      </form>
      {ended && (
        <p role="status" className="text-sm text-gray-400">
          {ended.links === 1
            ? `The link ${ended.email} hadn't used has stopped working: only a server admin makes them a new one.`
            : `The ${ended.links} links ${ended.email} hadn't used have stopped working: only a server admin makes them a new one.`}
        </p>
      )}
      {add.error && (
        <p role="alert" className="text-sm text-red-400">
          {add.error instanceof ApiError && add.error.code === "user_not_found"
            ? "Nobody signs in here with that email."
            : said(add.error, "Try again.")}
        </p>
      )}
    </section>
  );
}
