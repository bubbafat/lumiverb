# ADR-025: Visits: what's counted on public pages, and how, privately

## Status

Accepted (2026-10-29). Robert, after asking for analytics with Google Analytics as a plug-in: "I agree with your reasoning. I think downloads/exports are also an important metric (especially for public projects). Put GA aside for now", then "Let's build the metrics on what exists today - as we add new features we can figure out how/if metrics apply."

## Progress

| Part | Description | Status |
|------|-------------|--------|
| 1 | Counting, daily totals and the reports: the server, its API and `lumiverb visits` | Built |
| 2 | The web app: a project's and a library's visits, the account's, and where visitors came from | Not started |

## Overview

People share a project or a whole library with others through a public page, and an install can be a demo anyone tries. Their owners want to know whether anyone looks: how many came, what they opened and played, and which projects were exported.

Lumiverb is privacy-first, which rules out the usual way of counting:

- **No script from another site.** A page's scripts can read the sign-in token the web app keeps, and act as whoever is signed in. Google's script on every page would trust Google with every session, and a plug-in that adds scripts to pages would hand that to any plug-in's author, undoing ADR-023's reason for running plug-ins outside Lumiverb. The web app's own Content-Security-Policy loads scripts from itself only, and lumiverb.io's pages promise "no fonts, scripts or counters" from anywhere else.
- **No addresses that say too much.** A signed-in page's address holds searches, library and clip ids and filters that can name people; a counter that sends addresses elsewhere sends those.
- **No cookies,** and so no consent banner.

So Lumiverb counts for itself, on the server, what a public page's visitor asks of it.

## Decisions

| # | Question | Decision |
|---|----------|----------|
| 1 | What's counted? | What a public page's visitor (not signed in) does, by place, a public project or a public library: its page seen (`view`), a clip opened (`open`) and a video played (`play`, a video's player asking for it). A project exported (`export`, the editing apps' bins, signed in). A demo started (`demo`, Try it as a viewer, by the account the demo's viewer is in). What exists today only: what's added later says whether and how it's counted then. |
| 2 | Who's a visitor? | A visitor each day: a hash of the address nginx saw and the browser's user agent (an exporter's, their user id), keyed with a salt made for the day and deleted once the day is added up. Nothing kept says who anyone is: the hash can't be turned back into an address once its salt is gone, and while the day lasts the hash is all that's kept. |
| 3 | Repeats | The same visitor doing the same thing to the same place or clip within 30 minutes counts once: a page refreshed, the window focused again, a video's link renewed. An export or a demo counts every time. |
| 4 | Bots | A user agent that says it's a crawler, a link preview or a script (`bot`, `crawl`, `spider`, `curl`…) is counted as a bot, apart from visitors. Most never ask the API anything, as they run no scripts. |
| 5 | Where visitors came from | The site a page was opened from (its host only, `facebook.com`), sent by the page on its first request (`from`), since the API's own requests come from the page itself; kept for a visitor's first view of the day. |
| 6 | What's kept | Today: a row a visitor, place, action and clip (`visits_today`) with its count and last time. Every day before today: totals only (`visit_days`: visitors, times and bots by place, action and clip; `visit_sources`: visitors by the site they came from), added up by upkeep, after which the day's visitors and its salt are deleted. A place's visits go when it's deleted for good. |
| 7 | Who sees them? | A project's: its owner and the account's admins. A library's: its admins. The account's, every place at once with the demos: its admins. The server's own (`lumiverb visits`) is the account admin's on the machine too, for an install like the demo whose admin pages aren't reachable from elsewhere. |
| 8 | Never | Counting never fails a page: a visit that can't be counted is logged and the page answers as before. Nothing is counted for a signed-in person looking at a public page, the place's own people included. |
| 9 | Plug-ins and other services | Later, if wanted: the server could hand each counted visit to a plug-in (ADR-023, out of process) to forward elsewhere, Google's server-side protocol, Plausible, Umami; Lumiverb deciding what leaves. Not built. Google Analytics' own script is set aside. |

## Design

### Counting (part 1)

`src/server/visits.py` holds the rules: the day (the server's own date), the visitor's hash, bots, a `from` host checked (letters, digits, dots and hyphens, a dot in it, at most 253, `www.` left off; anything else is nothing), the 30-minute window, and `count(...)`, which each route calls after it has answered its checks: `GET /v1/public/projects/{id}` (`view`), `GET /v1/libraries/{id}` for a public request (`view`), `GET /v1/assets/{id}` and `GET /v1/assets/{id}/playback` for a public request (`open`, and `play` for a video), `GET /v1/projects/{id}/export` (`export`), `POST /v1/auth/demo` (`demo`). It writes in a savepoint and commits; anything going wrong is logged and the request goes on.

`src/server/repository/visits.py` (`VisitRepository`) holds the SQL: the day's salt (made once, `visit_salts`), a visit (one upsert, its count raised unless within the window), the adding up of days before today, and the reports.

### Adding up (part 1)

Upkeep (`POST /v1/upkeep`, every account) adds up every day before today into `visit_days` and `visit_sources` and deletes that day's `visits_today` rows and salt, in one transaction. A report reads both, so it's the same before and after.

### Reports (part 1)

- `GET /v1/projects/{id}/visits?days=30` (its owner, an account admin) and `GET /v1/libraries/{id}/visits?days=30` (its admins): each day's visitors and times by action, the totals, its clips most opened and played, the sites visitors came from, and bots.
- `GET /v1/visits?days=30` (account admins): every place with visits in those days, its name, and its totals; the demos.
- `lumiverb visits [--days N] [--project ID | --library ID]`.

A total's visitors are each day's added up: a visitor who comes back the next day counts again, since nothing links one day to the next.

### The web app (part 2)

A public project's page for its owner and Library settings for a public library: the numbers for the last 30 days, a day at a time, the clips most opened and where visitors came from; the Admin page: every place's, and the demos'. A public page sends `from` on its first request.
