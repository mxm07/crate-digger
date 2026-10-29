# Context for Claude

Crate Digger surfaces obscure, playable records (vinyl LPs, 78s) from archive.org and other sources as sampling material for producers. See README.md for the full pipeline and roadmap.

## Decisions already made
- **Rights:** surface everything accessible; never filter out a record for rights reasons. Every item keeps `clearance_required: true` and the clearance warning text. `rights_hint` is a UI badge only.
- **Discogs:** use the monthly CC0 XML dumps for bulk joins, not the API (about 60 requests/min when authenticated). Use the API only for small follow-up lookups such as "have" counts on a shortlist.
- **Being polite to archive.org:** identify the crawler in the User-Agent with a contact address, use the shared rate limiter in `Client`, and back off on 429/5xx responses.

## Conventions
- Python 3, `requests` only for the crawler; keep tests offline (fake session, no network).
- Output is JSON Lines that can be resumed by rerunning; never rewrite `items.jsonl` in place.
- Run `python -m pytest -q` before committing.

## Open items
- Run a live `--limit 200` trial; check the full / preview_only / unknown split and tune `PREVIEW_MAX_SECONDS` (35s) if user uploads misclassify.
- Confirm default collection IDs on archive.org; find the Boston Public Library LP collection ID.
- Next: Discogs dump join for the obscurity score.
