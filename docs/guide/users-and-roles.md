# Users and roles

Everyone signs in with their own email and password, and has a role that says what they can do. This page covers inviting people, roles on the whole account and on one library, server admins, passwords and API keys.

## Roles

| Role | Can |
|---|---|
| Viewer | Browse, search and play what they can see, and keep their own ratings, favorites and saved searches |
| Editor | All of that, and change clips: descriptions, tags, locations, people's names, archive and trash. Make projects. Change a library's path filters, ask for a scan, move a library to the trash and restore it. Make API keys |
| Admin | All of that, and invite people and set their roles, add libraries, run every library, delete for good, make a library public, and change Settings › AI, Processing, Files, Playback, Devices and Plug-ins |

An admin here is the account's admin. A **server admin** looks after the install itself: see [Server admins](#server-admins).

## Inviting someone

Only an admin invites people.

1. Open **Admin**, then **Manage users**.
2. Under **Invite someone**, type their **Email** and choose a **Role**.
3. To have them see only the libraries you give them, tick **Only the libraries they're given a role on**.
4. Click **Make invite link**.
5. Click **Copy** and send them the link yourself, by message or email.

The link works once, for 7 days. Opening it, they choose their own password (at least 12 characters) and are signed in. If the first link doesn't open on their device, send the second one shown: the same link at the computer's network address instead of its name.

On a Mac, the link opens only on that Mac until Settings › Network lets other devices use Lumiverb. See [Network, phones and HTTPS](network.md).

Lost the link, or it ran out? In the users list, click **New link** beside them. The old one stops working. Once they've signed in, there's no link to make: they use Forgot password.

From the command line, an admin's key can do the same:

```bash
lumiverb user create --email them@example.com --role editor
```

```bash
lumiverb user invite --email them@example.com
```

## Changing or removing someone

In **Admin › Manage users**:

- **Role**: choose another. You can't change your own, and the last admin can't be made anything else. API keys they made with a higher role than their new one stop working.
- **Libraries**: **Every library**, or **Only those given** (see [Seeing only what they're given](#seeing-only-what-theyre-given)).
- **Remove**: they can no longer sign in. Their ratings, saved searches and API keys go with them. You can't remove yourself or the last admin.

## Roles on one library

A person's account role holds on every library unless that library says otherwise. Its admin sets that in **Library settings › People**. An account admin is every library's admin.

Each person has a **Role here**:

- **Default**: what they'd have without a choice (shown in brackets).
- **Admin**: runs this library: its settings, its folder, who reads it, pausing its scans, location guesses, who sees it.
- **Editor** or **Viewer**: as in the account, on this library only.
- **No access**: they don't see it.

So an editor on one library can be a viewer everywhere else, or a viewer can be given one library to edit. The **Why** column says where each person's role comes from.

**Private** makes the library visible only to account admins and the people given a role on it. A public link is separate: anyone with it sees the library either way.

From the command line, with the library's name:

```bash
lumiverb library role --name "Family" --email them@example.com --role editor
```

```bash
lumiverb library private --name "Family" --on
```

## Seeing only what they're given

Someone set to **Only those given** sees no library until a library's admin gives them a role on it. It suits a client or a relative who should see one library and nothing else. Admins always see every library.

```bash
lumiverb user libraries --email them@example.com --only-given
```

## Server admins

What belongs to the whole install rather than one account is a server admin's: the accounts on it, where previews are kept (Settings › Storage), the network (Settings › Network), backups (Settings › Backups) and the log (Admin › Logs). The admin made when Lumiverb was set up is one.

A server admin adds others in **Admin › Accounts**, under **Server admins**: type the email of someone who signs in here and click **Make server admin**. **Remove** takes it away; the last one can't be removed there.

On the computer Lumiverb is installed on, the command line lists them, and adds or removes one even when there's none left. On a Mac:

```bash
lumiverb-server server-admins add you@example.com --env "$(brew --prefix)/etc/lumiverb/env"
```

On Linux:

```bash
sudo /opt/lumiverb/.venv/bin/lumiverb-server server-admins add you@example.com --env /etc/lumiverb/env
```

**Admin › Accounts** also makes a new account, with its own libraries, people and settings. Its first admin gets an invite link.

## Passwords

A password has at least 12 characters.

**Forgot password?** on the sign-in page works three ways:

- On a Mac, on that Mac itself: type your email and click **Continue**, and you go straight to choosing a new password. No email is needed.
- Where the install's env file has its `SMTP_*` lines filled in, it emails a reset link.
- Otherwise it says password reset isn't set up here.

Whoever looks after the install can always set a new password on the machine. It asks for the new password twice. On a Mac:

```bash
lumiverb-server reset-password --env "$(brew --prefix)/etc/lumiverb/env" --email them@example.com
```

On Linux:

```bash
sudo /opt/lumiverb/.venv/bin/lumiverb-server reset-password --env /etc/lumiverb/env --email them@example.com
```

A new password signs that person out everywhere, stops the API keys they made, and voids any reset or invite link they hadn't used.

To change your own password, sign out and use **Forgot password?** on the sign-in page.

## API keys

The command line and scripts sign in with an API key instead of a password. Editors and admins make them in **Settings › API Keys**:

1. Under **Create key**, give it a **Label** (where it's used) and a **Role**, at most your own.
2. Copy the key it shows. It isn't shown again.

Then give it to the command line (it asks you to paste the key):

```bash
lumiverb config set --api-url http://your-server:6464 --api-key -
```

The list shows when each key was last used. **Revoke** stops one. A key also stops when the person who made it gets a new password, is given a lower role, or is removed.

An admin can end keys nobody uses: under **Unused keys**, tick **End keys not used for** a number of days, then **Save**. If some keys would end at once, it names them and asks first. Keys Lumiverb makes for itself, devices' keys and the last admin key are never ended this way.

More in [The command line](command-line.md).
