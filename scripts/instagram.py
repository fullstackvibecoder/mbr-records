"""Instagram enumeration helpers.

Thin wrapper around `instaloader` so the rest of the pipeline can stay
platform-agnostic.  Enumeration uses instaloader; audio download still
uses yt-dlp because (a) it works on single-reel URLs even when profile
crawl is broken, and (b) it shares the audio path with the TikTok flow.

Auth: instaloader needs a logged-in session to enumerate reliably (IG
aggressively rate-limits anonymous scraping).  Two ways to supply it:

  - `INSTALOADER_SESSION_FILE`  → path to an `instaloader -l <user>`
    session file on disk.
  - `INSTAGRAM_USERNAME` + `INSTAGRAM_SESSION_B64` → base64 of a session
    file's contents (suited for GitHub Actions secrets); the file is
    materialised to a tmp path before use.

Either path resolves to an Instaloader bound to the chosen user.

For CLI use: `instaloader --login=<username>` once on a trusted machine,
then commit the resulting session file (NOT to git!) and point the env
var at it.  See README for details.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterator

try:
    import instaloader
except ImportError:
    print("instaloader not installed.  pip install instaloader", file=sys.stderr)
    raise


# Same handle on both platforms (verify with `instaloader --no-pictures
# --no-videos --no-metadata-json georgebrettolson` if uncertain).
DEFAULT_HANDLE = "georgebrettolson"


def _load_session_from_b64(loader: instaloader.Instaloader, username: str, b64: str) -> None:
    raw = base64.b64decode(b64)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".session") as f:
        f.write(raw)
        tmp_path = f.name
    try:
        loader.load_session_from_file(username, tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def get_loader() -> instaloader.Instaloader:
    """Return an authenticated Instaloader. Raises if no auth available."""
    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )

    session_file = os.environ.get("INSTALOADER_SESSION_FILE")
    if session_file and Path(session_file).exists():
        # instaloader needs the username to associate the session
        username = os.environ.get("INSTAGRAM_USERNAME")
        if not username:
            raise RuntimeError(
                "INSTALOADER_SESSION_FILE set but INSTAGRAM_USERNAME missing; "
                "instaloader needs the username to bind a session."
            )
        loader.load_session_from_file(username, session_file)
        return loader

    session_b64 = os.environ.get("INSTAGRAM_SESSION_B64")
    username = os.environ.get("INSTAGRAM_USERNAME")
    if session_b64 and username:
        _load_session_from_b64(loader, username, session_b64)
        return loader

    raise RuntimeError(
        "No Instagram session available. Set INSTALOADER_SESSION_FILE (+ "
        "INSTAGRAM_USERNAME), or INSTAGRAM_SESSION_B64 (+ INSTAGRAM_USERNAME)."
    )


def enumerate_reels(
    handle: str = DEFAULT_HANDLE,
    since: _dt.datetime | None = None,
) -> list[dict]:
    """Return reels from a profile as a list of dicts.

    Each dict shape mirrors the TikTok enumeration:
      { id: <shortcode>, upload_date: 'YYYYMMDD',
        webpage_url: 'https://www.instagram.com/reel/<shortcode>/',
        caption: <str>, timestamp: <int> }

    `since` filters to posts at-or-after that datetime (UTC).  Results are
    sorted oldest-first to match TikTok ordering downstream.
    """
    loader = get_loader()
    profile = instaloader.Profile.from_username(loader.context, handle)

    rows: list[dict] = []
    for post in profile.get_posts():
        # We only care about reels (single-video posts).  Profile.get_posts()
        # also returns images and carousels; skip them.
        if not post.is_video:
            continue
        taken = post.date_utc.replace(tzinfo=_dt.timezone.utc)
        if since and taken < since:
            break  # posts come newest-first; once older than since, stop
        rows.append({
            "id": post.shortcode,
            "upload_date": taken.strftime("%Y%m%d"),
            "webpage_url": f"https://www.instagram.com/reel/{post.shortcode}/",
            "caption": post.caption or "",
            "timestamp": int(taken.timestamp()),
        })
    rows.sort(key=lambda r: r["timestamp"])
    return rows


# ---------------------------------------------------------------------------
# Transcript-overlap scoring (used by both matcher and cron dedupe)
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"[a-z0-9']+")
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "for", "with", "by", "from", "as", "that", "this",
    "it", "i", "we", "you", "they", "he", "she", "our", "your", "their",
    "so", "if", "not", "no", "yes", "all", "any", "some", "will", "would",
    "uh", "um", "like", "just",
})


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS}


def transcript_jaccard(a: str, b: str) -> float:
    """Bag-of-words Jaccard over stopword-filtered tokens. 0.0–1.0."""
    sa, sb = _tokens(a), _tokens(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


if __name__ == "__main__":
    # Quick CLI: `python scripts/instagram.py [handle] [since-iso]`
    handle = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HANDLE
    since = None
    if len(sys.argv) > 2:
        since = _dt.datetime.fromisoformat(sys.argv[2])
        if since.tzinfo is None:
            since = since.replace(tzinfo=_dt.timezone.utc)
    rows = enumerate_reels(handle, since)
    print(json.dumps(rows, indent=2))
