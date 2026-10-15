# ADR-019: Search in PostgreSQL

## Status

Accepted (Robert, 2026-10-15). Replaces Quickwit as Lumiverb's text search.

## Progress

| Step | What | Status |
|------|------|--------|
| 1 | Search reads and writes the account's database; Quickwit is no longer used (its service still runs) | This change |
| 2 | Quickwit leaves the install: `lumiverb-server` no longer starts it, `init` no longer configures it, Homebrew no longer installs it, Linux's scripts no longer deploy it; its data folder goes | Next |

## Context

A test search on the live install (Robert's Mac) found 1 of 14,608 clips in Quickwit's clip index, 577 of about 15,800 scenes and 15 transcript segments. Search had worked from 2026-10-11 until 03:48 on 2026-10-15.

The cause was how Lumiverb used Quickwit. Quickwit has no update: the search sync replaced a clip's document by asking Quickwit to delete the clip's documents, then sending the new one. Quickwit's own documentation says why that can't work:

- a delete runs on every index file made before the delete, and a file can be started before the delete and still take the new document;
- deletes run only once a file is mature (48 hours by default), so the damage shows days later: the live file matured at 03:48 on the 15th, 48 hours after the last sync, and lost everything;
- deletes are meant for occasional removals (GDPR), "a few queries per hour or day"; the sync made 4,487 in two days.

A throwaway Quickwit with 10-second maturity reproduced it with the production code and real documents: the delete-then-send pattern kept 3 of 300 new documents sent one at a time, and 64 of 1,000 sent 8 at a time; a delete after a send removed exactly what it should.

Beyond the bug, Quickwit is built for something else: enormous, append-only logs and traces on object storage. Lumiverb's search is a small collection per account (thousands to a few hundred thousand clips) that changes all the time (descriptions arrive and are redone, people correct them, write notes, move, trash and restore clips) on one machine. Quickwit was a second service to install, start and upgrade, and a second copy of the data, kept in step by a sync that drifted silently: the database said 14,608 clips were in search.

## Decision

Text search runs in each account's own PostgreSQL database, beside the data it searches.

1. **What's searched**
   - **A clip's own words:** a row in `asset_search`, written by the search sync (`src/server/search/sync.py`): its description and tags as a person sees them (a correction replaces the machine's), the text read in it, its note, its transcript and its path's words. PostgreSQL computes its search vectors from them: one for the account's own search, one for a public page (no notes or paths), one for a public page that plays only the start of a video (no transcript either), each with its GIN index.
   - **A scene's:** computed with its row in `video_scenes` from its description and tags. Nothing to keep in step.
   - **A transcript's:** `transcript_segments`, one row a segment with its times, rewritten with the transcript in one transaction.
2. **How it matches** (`src/server/search/text_search.py`, the only place that knows the engine):
   - words are English and stemmed ("dogs" finds "dog"), a path's too (a folder "Family" is found by "family"), as typed words are read the same way;
   - unquoted words: any of them matches, more of them ranks higher, and all of them in a row higher still;
   - a quoted phrase must match, its words in order;
   - words of three letters or more also match as a prefix, at half the score;
   - descriptions, tags and notes weigh most, then the text read in a picture and transcripts, then paths;
   - several search terms must each match.
3. **What's in sight** is the database's: search joins the clips in sight, so a clip trashed or archived isn't found, and is found again when restored, with nothing deleted from or written back to search. Deleting a clip for good deletes its rows with it.
4. **Writes** are rows in the same database, each marked synced in the same transaction:
   - a save that changes one clip's words writes its row (`try_sync_asset`);
   - a batch of descriptions or OCR writes its clips' rows after its commit (`sync_clips`);
   - upkeep's sweep writes any still stale, by the same rule as before (`STALE_SEARCH`).
   A restored backup holds its own search; there's nothing to remake.
5. **ParadeDB later, if needed.** PostgreSQL's ranking doesn't weigh rare words more (BM25 does). If that proves to matter, ParadeDB's `pg_search` brings BM25 into PostgreSQL. It has its own index and operators, so it would be a migration and `text_search.py`, nothing else; it's our choice to make and package, not one a user drops in (it must match Lumiverb's PostgreSQL on Mac and Linux, and it's AGPL).

## Consequences

- One source of truth: search finds what the database holds, as of the last write to a clip's row (moments after a save).
- No search service: one less process, port, data folder and upgrade; a backup and restore covers search.
- What was Quickwit's goes:
  - its client, index rebuilds, the Postgres ILIKE fallback, the Admin page's Quickwit states and fallback notes;
  - routes only it needed answer 410 `gone`: `POST /v1/upkeep/recreate-search-indexes` (a search sync with `force=true` writes everything again) and `POST /v1/video/scenes/{id}/sync` (a scene is searchable once saved);
  - the install's Quickwit goes in step 2.
- The migration clears every clip's and transcript's sync time; the next upkeep sweeps write them from the database (no AI work runs again), 30 seconds a sweep per account.
- Ranking changes in its details (PostgreSQL's ranking, not Quickwit's BM25); the order of what matters (field weights, phrases, prefixes below whole words) is kept and tested (`tests/test_text_search.py`).
