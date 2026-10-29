#!/usr/bin/env python3
"""
Virtual Crate Digger — archive.org inventory crawler (step 1 + 2 of the pipeline).

What it does
  1. Lists candidate record items (vinyl LPs, 78s) from archive.org using the
     Scrape API, across configurable collections and keyword sweeps.
  2. Fetches each item's metadata JSON, groups its audio files into tracks,
     and classifies playback as "full", "preview_only" or "unknown".
  3. Writes one JSON line per item (resumable) plus a flat tracks CSV.

Every item is kept and tagged with a clearance warning: sampling it in a
released work requires the artist to clear it. `rights_hint` is only a hint
for UI badges, never a clearance.

Usage
  pip install requests
  python inventory.py --contact you@example.com --out data/
  python inventory.py --contact you@example.com --out data/ --limit 200   # quick trial
  python inventory.py --contact you@example.com --out data/ --collections unlockedrecordings

Re-running with the same --out resumes: items already in items.jsonl are skipped.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Iterable, Iterator

import requests

SCRAPE_URL = "https://archive.org/services/search/v1/scrape"
METADATA_URL = "https://archive.org/metadata/{identifier}"
DOWNLOAD_URL = "https://archive.org/download/{identifier}/{filename}"
DETAILS_URL = "https://archive.org/details/{identifier}"

# Collections to sweep by default. Collection IDs on archive.org change rarely,
# but verify these on the site and extend the list as you discover more.
DEFAULT_COLLECTIONS = [
    "unlockedrecordings",  # out-of-print LPs with full audio
    "georgeblood",         # Great 78 Project transfers
    "78rpm",               # broader 78rpm parent collection
]

# Keyword sweeps across all audio, to catch hobbyist vinyl rips outside the
# big collections. Each becomes its own Scrape API query.
DEFAULT_KEYWORD_QUERIES = [
    'mediatype:audio AND (subject:"vinyl rip" OR title:"vinyl rip" OR description:"vinyl rip")',
    'mediatype:audio AND (subject:"needle drop" OR description:"needle drop")',
    'mediatype:audio AND (subject:"library music" OR subject:"private press")',
    'mediatype:audio AND (subject:LP OR subject:vinyl) AND (subject:"33 rpm" OR subject:"33rpm")',
]

AUDIO_FORMATS = {
    "VBR MP3", "128Kbps MP3", "64Kbps MP3", "MP3",
    "Flac", "24bit Flac", "Ogg Vorbis", "WAVE", "Apple Lossless Audio", "AIFF",
}
STREAM_FORMAT_PREFERENCE = ["VBR MP3", "128Kbps MP3", "MP3", "Ogg Vorbis", "64Kbps MP3"]

# A track at or under this many seconds is treated as a preview clip.
PREVIEW_MAX_SECONDS = 35.0

CLEARANCE_WARNING = (
    "Not pre-cleared. Free to listen and experiment with, but if you release a "
    "track that samples this recording, you are responsible for clearing the sample."
)


# --------------------------------------------------------------------------- HTTP

class Client:
    """Polite HTTP client: shared rate limit, retries with backoff."""

    def __init__(self, contact: str, min_interval: float = 0.25,
                 session: requests.Session | None = None, max_retries: int = 5):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = (
            f"VirtualCrateDigger/0.1 (inventory crawler; contact: {contact})"
        )
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._lock = threading.Lock()
        self._next_ok = 0.0

    def _wait_turn(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next_ok - now
            self._next_ok = max(now, self._next_ok) + self.min_interval
        if delay > 0:
            time.sleep(delay)

    def get_json(self, url: str, params: dict | None = None) -> dict:
        for attempt in range(self.max_retries):
            self._wait_turn()
            try:
                resp = self.session.get(url, params=params, timeout=60)
            except requests.RequestException as exc:
                err: Exception | str = exc
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (429, 500, 502, 503, 504):
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        time.sleep(int(retry_after))
                    err = f"HTTP {resp.status_code}"
                else:
                    resp.raise_for_status()
                    err = f"HTTP {resp.status_code}"
            time.sleep(min(60, 2 ** attempt))
        raise RuntimeError(f"Giving up on {url} after {self.max_retries} attempts: {err}")


# --------------------------------------------------------------------------- listing

def scrape_identifiers(client: Client, query: str, limit: int | None = None,
                       page_size: int = 1000) -> Iterator[dict]:
    """Yield {'identifier', ...} rows for a query via the Scrape API (cursor paging)."""
    params = {
        "q": query,
        "fields": "identifier,title,mediatype",
        "count": max(100, min(page_size, 10000)),  # API requires count >= 100
    }
    seen = 0
    cursor = None
    while True:
        if cursor:
            params["cursor"] = cursor
        data = client.get_json(SCRAPE_URL, params=params)
        for row in data.get("items", []):
            yield row
            seen += 1
            if limit is not None and seen >= limit:
                return
        cursor = data.get("cursor")
        if not cursor:
            return


def build_queries(collections: list[str], keyword_queries: list[str]) -> list[tuple[str, str]]:
    """Return (source_label, query) pairs."""
    queries = [(f"collection:{c}", f"collection:{c} AND mediatype:audio") for c in collections]
    queries += [(f"keywords:{i + 1}", q) for i, q in enumerate(keyword_queries)]
    return queries


# --------------------------------------------------------------------------- parsing

def parse_length(value) -> float | None:
    """archive.org 'length' is seconds ('183.42') or clock time ('3:03', '1:02:03')."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        if ":" in s:
            total = 0.0
            for part in s.split(":"):
                total = total * 60 + float(part)
            return total
        return float(s)
    except ValueError:
        return None


def as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def first(value):
    vals = as_list(value)
    return vals[0] if vals else None


def extract_year(meta: dict) -> int | None:
    for key in ("year", "date", "publicdate"):
        for v in as_list(meta.get(key)):
            m = re.search(r"\b(18[5-9]\d|19\d\d|20\d\d)\b", str(v))
            if m:
                return int(m.group(1))
    return None


def _stem(name: str) -> str:
    base = os.path.basename(name)
    base = re.sub(r"\.(mp3|flac|ogg|wav|m4a|aiff?|afpk|png)$", "", base, flags=re.I)
    return re.sub(r"_(sample|vbr|64kb|128kb)$", "", base, flags=re.I)


def group_tracks(identifier: str, files: list[dict]) -> list[dict]:
    """Collapse the many derivative files of one recording into a single track row."""
    groups: dict[str, list[dict]] = {}
    for f in files:
        if f.get("format") not in AUDIO_FORMATS:
            continue
        groups.setdefault(_stem(f.get("name", "")), []).append(f)

    tracks = []
    for stem, fs in groups.items():
        lengths = [l for l in (parse_length(f.get("length")) for f in fs) if l]
        formats = sorted({f.get("format") for f in fs})
        stream = None
        for pref in STREAM_FORMAT_PREFERENCE:
            stream = next((f for f in fs if f.get("format") == pref), None)
            if stream:
                break
        stream = stream or fs[0]
        titled = next((f for f in fs if f.get("title")), {})
        track_no = next((f.get("track") for f in fs if f.get("track")), None)
        tracks.append({
            "stem": stem,
            "title": titled.get("title") or stem,
            "track": track_no,
            "length_sec": round(max(lengths), 2) if lengths else None,
            "formats": formats,
            "stream_url": DOWNLOAD_URL.format(identifier=identifier, filename=stream["name"]),
            "is_sample_file": bool(re.search(r"_sample\.", stream.get("name", ""), re.I)),
        })

    def sort_key(t):
        m = re.match(r"\d+", str(t["track"] or ""))
        return (int(m.group()) if m else 10**6, t["stem"])

    return sorted(tracks, key=sort_key)


def classify_playback(meta: dict, tracks: list[dict]) -> tuple[str, str]:
    """Return (playback, reason)."""
    if str(meta.get("access-restricted-item", "")).lower() == "true":
        return "preview_only", "item is access-restricted"
    if not tracks:
        return "unknown", "no audio files found"
    lengths = [t["length_sec"] for t in tracks if t["length_sec"]]
    if tracks and all(t["is_sample_file"] for t in tracks):
        return "preview_only", "only _sample files present"
    if not lengths:
        return "unknown", "no track lengths in metadata"
    if max(lengths) <= PREVIEW_MAX_SECONDS:
        return "preview_only", f"all tracks <= {PREVIEW_MAX_SECONDS:.0f}s"
    return "full", "full-length audio files present"


def rights_hint(meta: dict, year: int | None) -> str:
    """A UI hint only. Everything still carries the clearance warning."""
    license_url = str(meta.get("licenseurl") or "").lower()
    if "publicdomain" in license_url:
        return "marked_public_domain"
    if "creativecommons.org" in license_url:
        return "creative_commons"
    if year is not None and year <= 1925:
        return "likely_public_domain_us"
    return "uncleared"


def build_record(identifier: str, payload: dict, sources: list[str]) -> dict:
    meta = payload.get("metadata", {}) or {}
    files = payload.get("files", []) or []
    tracks = group_tracks(identifier, files)
    playback, reason = classify_playback(meta, tracks)
    year = extract_year(meta)
    total = sum(t["length_sec"] or 0 for t in tracks)
    return {
        "identifier": identifier,
        "url": DETAILS_URL.format(identifier=identifier),
        "title": first(meta.get("title")),
        "creator": as_list(meta.get("creator")),
        "date": first(meta.get("date")),
        "year": year,
        "publisher": first(meta.get("publisher")),
        "subjects": as_list(meta.get("subject")),
        "collections": as_list(meta.get("collection")),
        "found_via": sources,
        "license_url": meta.get("licenseurl"),
        "playback": playback,
        "playback_reason": reason,
        "track_count": len(tracks),
        "total_length_sec": round(total, 2),
        "rights_hint": rights_hint(meta, year),
        "clearance_required": True,
        "clearance_warning": CLEARANCE_WARNING,
        "is_dark": bool(payload.get("is_dark")),
        "tracks": tracks,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# --------------------------------------------------------------------------- crawl

@dataclass
class Stats:
    listed: int = 0
    skipped_existing: int = 0
    fetched: int = 0
    failed: int = 0
    playback: dict = field(default_factory=dict)


def load_done(path: str) -> set[str]:
    done = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    done.add(json.loads(line)["identifier"])
                except (json.JSONDecodeError, KeyError):
                    continue
    return done


def collect_candidates(client: Client, queries: list[tuple[str, str]],
                       limit: int | None) -> dict[str, list[str]]:
    """identifier -> list of sources it was found via (dedupes across queries)."""
    found: dict[str, list[str]] = {}
    for label, q in queries:
        n = 0
        try:
            for row in scrape_identifiers(client, q, limit=limit):
                found.setdefault(row["identifier"], []).append(label)
                n += 1
        except Exception as exc:  # keep going if one query fails
            print(f"  ! listing failed for {label}: {exc}", file=sys.stderr)
        print(f"  {label}: {n} items", file=sys.stderr)
    return found


def write_tracks_csv(items_path: str, csv_path: str) -> int:
    cols = ["identifier", "album_title", "creator", "year", "playback", "rights_hint",
            "track", "track_title", "length_sec", "stream_url", "clearance_warning"]
    rows = 0
    with open(items_path, encoding="utf-8") as fin, \
            open(csv_path, "w", newline="", encoding="utf-8") as fout:
        w = csv.DictWriter(fout, fieldnames=cols)
        w.writeheader()
        for line in fin:
            rec = json.loads(line)
            for t in rec["tracks"]:
                w.writerow({
                    "identifier": rec["identifier"],
                    "album_title": rec["title"],
                    "creator": "; ".join(map(str, rec["creator"])),
                    "year": rec["year"],
                    "playback": rec["playback"],
                    "rights_hint": rec["rights_hint"],
                    "track": t["track"],
                    "track_title": t["title"],
                    "length_sec": t["length_sec"],
                    "stream_url": t["stream_url"],
                    "clearance_warning": rec["clearance_warning"],
                })
                rows += 1
    return rows


def run(client: Client, out_dir: str, queries: list[tuple[str, str]],
        limit: int | None = None, workers: int = 4) -> Stats:
    os.makedirs(out_dir, exist_ok=True)
    items_path = os.path.join(out_dir, "items.jsonl")
    failed_path = os.path.join(out_dir, "failed.txt")
    stats = Stats()

    print("Listing candidates…", file=sys.stderr)
    candidates = collect_candidates(client, queries, limit)
    stats.listed = len(candidates)

    done = load_done(items_path)
    todo = [i for i in candidates if i not in done]
    stats.skipped_existing = len(candidates) - len(todo)
    print(f"{stats.listed} unique items; {stats.skipped_existing} already done; "
          f"fetching {len(todo)}", file=sys.stderr)

    write_lock = threading.Lock()

    def fetch(identifier: str) -> dict:
        payload = client.get_json(METADATA_URL.format(identifier=identifier))
        return build_record(identifier, payload, candidates[identifier])

    with open(items_path, "a", encoding="utf-8") as out, \
            open(failed_path, "a", encoding="utf-8") as failed, \
            ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, i): i for i in todo}
        for n, fut in enumerate(as_completed(futures), 1):
            ident = futures[fut]
            try:
                rec = fut.result()
            except Exception as exc:
                stats.failed += 1
                with write_lock:
                    failed.write(f"{ident}\t{exc}\n")
                continue
            with write_lock:
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
            stats.fetched += 1
            stats.playback[rec["playback"]] = stats.playback.get(rec["playback"], 0) + 1
            if n % 100 == 0:
                print(f"  {n}/{len(todo)} fetched", file=sys.stderr)

    rows = write_tracks_csv(items_path, os.path.join(out_dir, "tracks.csv"))
    print(f"Done. fetched={stats.fetched} failed={stats.failed} "
          f"playback={stats.playback} track_rows={rows}", file=sys.stderr)
    return stats


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--contact", required=True,
                   help="email or URL put in the User-Agent so archive.org can reach you")
    p.add_argument("--out", default="data", help="output directory (default: data)")
    p.add_argument("--collections", nargs="*", default=DEFAULT_COLLECTIONS,
                   help="collection identifiers to sweep")
    p.add_argument("--no-keywords", action="store_true", help="skip keyword sweeps")
    p.add_argument("--query", action="append", default=[],
                   help="extra raw archive.org query (repeatable)")
    p.add_argument("--limit", type=int, default=None,
                   help="max items per query (for trial runs)")
    p.add_argument("--workers", type=int, default=4, help="parallel metadata fetches")
    p.add_argument("--min-interval", type=float, default=0.25,
                   help="minimum seconds between any two requests (default 0.25)")
    args = p.parse_args(list(argv) if argv is not None else None)

    keyword_queries = [] if args.no_keywords else list(DEFAULT_KEYWORD_QUERIES)
    keyword_queries += args.query
    queries = build_queries(args.collections, keyword_queries)
    client = Client(args.contact, min_interval=args.min_interval)
    run(client, args.out, queries, limit=args.limit, workers=args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
