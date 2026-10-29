"""Offline tests for inventory.py — no network. Run: python -m pytest -q"""

import csv
import json
import os

import pytest

import inventory as inv


class FakeResp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.headers = data, status, {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, routes):
        self.routes, self.headers, self.calls = routes, {}, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        key = url
        if url == inv.SCRAPE_URL:
            key = (params["q"], params.get("cursor"))
        val = self.routes[key]
        if isinstance(val, list):  # sequence of responses (for retry tests)
            val = val.pop(0)
        return val if isinstance(val, FakeResp) else FakeResp(val)


FULL_LP = {
    "metadata": {"title": "Moog Moods for Bowling", "creator": "The Pinsetters",
                 "date": "1969", "collection": ["unlockedrecordings", "audio_music"],
                 "subject": ["library music", "moog"]},
    "files": [
        {"name": "01 - Strike.flac", "format": "Flac", "length": "183.42", "track": "01", "title": "Strike"},
        {"name": "01 - Strike.mp3", "format": "VBR MP3", "length": "3:03", "track": "01", "title": "Strike"},
        {"name": "02 - Gutter Ball.flac", "format": "Flac", "length": "201.1", "track": "02"},
        {"name": "02 - Gutter Ball.mp3", "format": "VBR MP3", "length": "201.1", "track": "02"},
        {"name": "cover.jpg", "format": "JPEG"},
    ],
}
PREVIEW_LP = {
    "metadata": {"title": "Big Label Hits", "date": "1978-05-01",
                 "access-restricted-item": "true", "collection": "boston_public_library"},
    "files": [{"name": "a1_sample.mp3", "format": "VBR MP3", "length": "30.0"}],
}
OLD_78 = {
    "metadata": {"title": "Dreamy Blues", "date": "1924", "collection": ["georgeblood", "78rpm"]},
    "files": [{"name": "dreamy-blues.mp3", "format": "VBR MP3", "length": "190"}],
}


def make_client(routes):
    return inv.Client("test@example.com", min_interval=0, session=FakeSession(routes), max_retries=3)


def test_parse_length():
    assert inv.parse_length("183.42") == 183.42
    assert inv.parse_length("3:03") == 183
    assert inv.parse_length("1:02:03") == 3723
    assert inv.parse_length("") is None and inv.parse_length("n/a") is None


def test_group_tracks_collapses_derivatives_and_prefers_mp3():
    tracks = inv.group_tracks("x", FULL_LP["files"])
    assert [t["title"] for t in tracks] == ["Strike", "02 - Gutter Ball"]
    assert tracks[0]["formats"] == ["Flac", "VBR MP3"]
    assert tracks[0]["stream_url"].endswith("01 - Strike.mp3")


def test_classification_and_rights_hints():
    full = inv.build_record("moog", FULL_LP, ["collection:unlockedrecordings"])
    prev = inv.build_record("hits", PREVIEW_LP, ["keywords:1"])
    old = inv.build_record("blues", OLD_78, ["collection:georgeblood"])
    assert full["playback"] == "full" and full["track_count"] == 2
    assert prev["playback"] == "preview_only"
    assert old["rights_hint"] == "likely_public_domain_us"
    assert full["rights_hint"] == "uncleared"
    assert all(r["clearance_required"] and r["clearance_warning"] for r in (full, prev, old))
    assert prev["collections"] == ["boston_public_library"]


def test_short_tracks_without_restriction_flag_are_preview():
    payload = {"metadata": {}, "files": [{"name": "a.mp3", "format": "VBR MP3", "length": "29.9"}]}
    assert inv.build_record("p", payload, [])["playback"] == "preview_only"


def test_cc_license_hint():
    payload = {"metadata": {"licenseurl": "https://creativecommons.org/licenses/by/4.0/"}, "files": []}
    rec = inv.build_record("cc", payload, [])
    assert rec["rights_hint"] == "creative_commons" and rec["playback"] == "unknown"


def test_scrape_paginates_with_cursor():
    q = "collection:unlockedrecordings AND mediatype:audio"
    client = make_client({
        (q, None): {"items": [{"identifier": "a"}, {"identifier": "b"}], "cursor": "C1"},
        (q, "C1"): {"items": [{"identifier": "c"}]},
    })
    assert [r["identifier"] for r in inv.scrape_identifiers(client, q)] == ["a", "b", "c"]
    assert [r["identifier"] for r in inv.scrape_identifiers(client, q, limit=2)] == ["a", "b"]


def test_retry_on_503(monkeypatch):
    monkeypatch.setattr(inv.time, "sleep", lambda s: None)
    url = inv.METADATA_URL.format(identifier="x")
    client = make_client({url: [FakeResp({}, 503), FakeResp({"ok": 1})]})
    assert client.get_json(url) == {"ok": 1}


def test_end_to_end_run_and_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(inv.time, "sleep", lambda s: None)
    q1 = "collection:unlockedrecordings AND mediatype:audio"
    q2 = "collection:georgeblood AND mediatype:audio"
    routes = {
        (q1, None): {"items": [{"identifier": "moog"}, {"identifier": "hits"}]},
        (q2, None): {"items": [{"identifier": "blues"}, {"identifier": "moog"}]},  # dup across queries
        inv.METADATA_URL.format(identifier="moog"): FULL_LP,
        inv.METADATA_URL.format(identifier="hits"): PREVIEW_LP,
        inv.METADATA_URL.format(identifier="blues"): OLD_78,
    }
    queries = inv.build_queries(["unlockedrecordings", "georgeblood"], [])
    stats = inv.run(make_client(routes), str(tmp_path), queries, workers=2)
    assert stats.listed == 3 and stats.fetched == 3 and stats.failed == 0
    assert stats.playback == {"full": 2, "preview_only": 1}

    recs = {json.loads(l)["identifier"]: json.loads(l) for l in open(tmp_path / "items.jsonl")}
    assert recs["moog"]["found_via"] == ["collection:unlockedrecordings", "collection:georgeblood"]
    rows = list(csv.DictReader(open(tmp_path / "tracks.csv")))
    assert len(rows) == 4 and all(r["clearance_warning"] for r in rows)

    # Second run resumes: nothing re-fetched.
    session = FakeSession(dict(routes))
    stats2 = inv.run(inv.Client("t", min_interval=0, session=session), str(tmp_path), queries)
    assert stats2.fetched == 0 and stats2.skipped_existing == 3
    assert not any("/metadata/" in c[0] for c in session.calls)
