# Crate Digger

A virtual crate-digging app: surfaces unique, little-known records (vinyl LPs and 78s) that are playable online, as sampling sources for producers.

## Status

**Step 1–2 of the pipeline: inventory.** `inventory.py` crawls archive.org, lists candidate record items, fetches their metadata, groups audio files into tracks, and tags each item's playback status.

## Quick start

```bash
pip install requests pytest
python -m pytest -q                                   # offline tests, no network
python inventory.py --contact you@example.com --out data/ --limit 200   # trial run
python inventory.py --contact you@example.com --out data/               # full crawl (resumable)
```

Options: `--collections` (archive.org collection IDs), `--query` (extra raw search, repeatable), `--no-keywords`, `--workers` (default 4), `--min-interval` (seconds between requests, default 0.25).

## Output

- `data/items.jsonl`: one record per item: title, creator, year, collections, `playback` (`full` / `preview_only` / `unknown`), `rights_hint`, clearance warning, and a list of tracks with stream URLs. Appended as it goes; reruns skip items already fetched.
- `data/tracks.csv`: one row per track, flattened for the app or a spreadsheet.
- `data/failed.txt`: identifiers whose metadata fetch failed, with the error.

## Sources

| Source | What it is |
|---|---|
| `unlockedrecordings` | Out-of-print LPs with full audio |
| `georgeblood`, `78rpm` | Great 78 Project shellac transfers |
| Keyword sweeps | Hobbyist uploads tagged "vinyl rip", "needle drop", "library music", "private press" |

The Boston Public Library LP collection is not in the defaults yet; its ID still needs confirming, and most of its items are 30-second previews.

## Rights policy

Every accessible recording is surfaced. Nothing is filtered out for rights reasons. Every item carries a clearance warning: it's free to listen to and experiment with, but an artist who releases a track sampling it is responsible for clearing the sample. `rights_hint` (`marked_public_domain`, `creative_commons`, `likely_public_domain_us`, `uncleared`) is a UI badge only, never a clearance.

## Roadmap

1. ~~Inventory archive.org~~ (done, pending a live trial run)
2. Deduplicate tracks with Chromaprint/AcoustID; link to MusicBrainz IDs
3. Obscurity score: join the Discogs monthly CC0 data dumps (never reissued, small/private label, not on streaming services, low "have" counts)
4. Sample-usefulness score: audio analysis (Essentia/librosa) for drum breaks, instrumental passages, BPM, key, surface noise. Store results per time segment.
5. More sources: UCSB Cylinder Audio Archive, Free Music Archive
6. The app itself
