# OSM TT_Editor hourly quality check

This is an adapted version of the `#tt_event`-family quality-check
pipeline, re-architected to track a **fixed list of OSM usernames**
instead of a `#hashtag`.

## What's different from the hashtag version

OSM's API supports filtering changesets directly by editor
(`display_name=`). So instead of scanning every changeset on the
planet each hour and checking its comment text for a hashtag (the
source of nearly every stuck-query, rate-limit, and pagination problem
the hashtag pipeline has run into), this version asks OSM directly:
"what did THIS specific editor close in this hour?" -- one precise,
low-volume query per tracked editor, 92 queries total per run instead
of an unbounded global scan.

**The 13th CSV column, `hashtag`, is kept for structural consistency
with every other repo in this family** -- but it's now a fixed label,
not a filter. Every row in this deployment reads `TT_Editor`,
regardless of which tracked editor the row came from (that's what the
`username` column is for).

## The tracked editors

92 usernames, listed in full in `config.py`'s `TRACKED_USERNAMES` list.
Edit that list to add, remove, or change who's being watched -- no
other code needs to change.

## What stayed the same

- All 17 check categories, unchanged
- The Overpass circuit breaker, retry queue, and fairness fix (skipped
  items get priority next run, so the same few items can't monopolize
  every retry attempt)
- The `covered=yes` exception, the floating-highway live-verification
  fix, and both misattribution fixes (a flagged pair must involve
  something the current changeset actually touched)
- Crash-safe checkpointing and a clean exit on unexpected errors

## What changed under the hood

- `fetch.py`: `fetch_changesets_in_window()` now loops over
  `config.TRACKED_USERNAMES`, querying each via `display_name=`,
  instead of paginating through every changeset on the planet and
  matching hashtag text client-side.
- The changeset-discovery retry queue (`pending_changeset_scans.json`)
  now scopes each stuck entry by **username + time-range**, not just
  time-range -- since a persistent failure here means "this editor's
  query is stuck," not "the whole planet's query is stuck."
- `main.py`'s `retry_pending_scans()` retries each stuck entry against
  that one specific editor only.

Verified with real tests before delivery: all 16 tracked usernames get
queried every run; a failure for one specific editor is correctly
isolated (the other 15 still succeed) and recorded with the right
username; the retry queue correctly re-queries only the failing editor
and clears the entry once resolved.

## Start date

`data/state.json` is seeded with `"last_run_end_utc":
"2026-10-01T00:00:00"`. The same 1-hour-per-run cap applies as every
other repo in this family -- see `main.py`'s `determine_window()` for
why -- so catching up from October 1, 2026 to today will take roughly
as many real-world hours as there are hours in that gap.

## Setup

1. Push this repository to GitHub.
2. Confirm `.github/workflows/hourly.yml` is at the repo root under
   `.github/workflows/`.
3. In **Settings → Actions → General → Workflow permissions**, select
   **Read and write permissions**.
4. Optionally add repository secrets: `OSMCHA_TOKEN`, `SLACK_BOT_TOKEN`,
   `SLACK_CHANNEL_ID`.
5. Trigger the workflow once manually to confirm a clean first run.

## Output

Same 13-column CSV structure as every other repo in this family:
`s_no, error_type, username, user_id, osm_location_link, changeset_id,
changeset_link, osm_object_type, osm_object_id, time_utc, country, detail,
hashtag`

`hashtag` is always `TT_Editor` for every row in this deployment.
`username` holds the actual tracked editor who made the edit.
